import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import cloud_digest  # noqa: E402
from tokenatlas import why  # noqa: E402

USAGE = {"input_tokens": 2, "output_tokens": 50, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300,
         "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
         "iterations": [{"input_tokens": 2}], "server_tool_use": {"web_search_requests": 0}}


def user(uuid, ts, text):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "s1", "cwd": "/home/user/repo",
            "message": {"role": "user", "content": text}}


def assistant(uuid, ts, req):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": "s1", "cwd": "/home/user/repo",
            "requestId": req, "entrypoint": "remote_projects", "version": "2.1.329",
            "message": {"id": "m" + req, "role": "assistant", "model": "claude-sonnet-5-5", "usage": USAGE,
                        "stop_reason": "end_turn", "content": [{"type": "text", "text": "SECRET ANSWER"}]}}


ROWS = [
    user("u1", "2026-11-20T10:00:00Z", "SECRET PROMPT"),
    assistant("a1", "2026-11-20T10:00:05Z", "req1"),
    {"type": "user", "uuid": "t1", "timestamp": "2026-11-20T10:00:06Z", "sessionId": "s1",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET TOOL"}]}},
    assistant("a2", "2026-11-20T10:00:09Z", "req2"),
    {"type": "attachment", "uuid": "x", "timestamp": "2026-11-20T10:00:10Z"},
]


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class CloudDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.transcript = self.tmp / "s1.jsonl"
        self.state = self.tmp / "state"
        self.tmp = self.tmp / "p"
        self.tmp.mkdir()
        self.transcript = self.tmp / "s1.jsonl"
        self.payload = {"transcript_path": str(self.transcript), "session_id": "s1"}

    def spool(self):
        return (self.state / "projects" / self.tmp.name / "s1.jsonl").read_text()

    def test_digest_drops_text_and_keeps_usage(self):
        write(self.transcript, ROWS)
        self.assertEqual(cloud_digest.run(self.payload, self.state), 3)  # one prompt, two requests
        text = self.spool()
        for secret in ("SECRET", "server_tool_use"):
            self.assertNotIn(secret, text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[1]["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"], 200)
        self.assertEqual(rows[1]["entrypoint"], "remote_projects")

    def test_offset_tailing_sends_each_line_once(self):
        write(self.transcript, ROWS[:2])
        self.assertEqual(cloud_digest.run(self.payload, self.state), 2)
        self.assertEqual(cloud_digest.run(self.payload, self.state), 0)
        with self.transcript.open("a") as fh:
            fh.write(json.dumps(ROWS[3]) + "\n" + '{"type": "assist')  # a half-written last line
        self.assertEqual(cloud_digest.run(self.payload, self.state), 1)
        self.assertEqual(len(self.spool().splitlines()), 3)

    def test_collector_reads_the_digest_like_the_transcript(self):
        write(self.transcript, ROWS)
        cloud_digest.run(self.payload, self.state)
        start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)

        def read(file):
            root = Path(tempfile.mkdtemp()) / "projects"
            (root / "-home-user-repo").mkdir(parents=True)
            (root / "-home-user-repo" / "s1.jsonl").write_bytes(file.read_bytes())
            return sorted((r.call_id, r.model, r.fresh_input, r.cache_read, r.cache_write, r.output, r.project)
                          for r in why.collect_claude(root, start, end))

        original, digest = read(self.transcript), read(self.state / "projects" / self.tmp.name / "s1.jsonl")
        self.assertEqual(len(original), 2)
        self.assertEqual(original, digest)


    def test_only_numeric_counters_survive(self):
        row = assistant("a9", "2026-11-20T10:00:05Z", "req9")
        row["message"]["usage"] = dict(USAGE, prompt="SECRET PROMPT", service_tier="standard",
                                       cache_creation={"ephemeral_1h_input_tokens": 200, "note": "SECRET NOTE"},
                                       iterations=[{"input_tokens": 2, "output_tokens": 3, "text": "SECRET ITER"}])
        write(self.transcript, [row])
        cloud_digest.run(self.payload, self.state)
        text = (self.state / "projects" / self.transcript.parent.name / "s1.jsonl").read_text()
        self.assertNotIn("SECRET", text)
        usage = json.loads(text)["message"]["usage"]
        self.assertEqual(usage["iterations"], [{"input_tokens": 2, "output_tokens": 3}])
        self.assertEqual(usage["service_tier"], "standard")

    def test_turn_boundaries_follow_the_collector(self):
        rows = [{"type": "user", "uuid": "i", "timestamp": "2026-11-20T10:00:00Z", "sessionId": "s1",
                 "message": {"role": "user", "content": [{"type": "image"}]}},
                {"type": "user", "uuid": "m", "timestamp": "2026-11-20T10:00:01Z", "sessionId": "s1",
                 "message": {"role": "user", "content": [{"type": "tool_result"}, {"type": "text", "text": "go on"}]}},
                {"type": "user", "uuid": "r", "timestamp": "2026-11-20T10:00:02Z", "sessionId": "s1",
                 "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}}]
        write(self.transcript, rows)
        self.assertEqual(cloud_digest.run(self.payload, self.state), 2)  # the image prompt and the mixed row, not the reminder

    def test_a_non_object_line_does_not_stall_the_hook(self):
        self.transcript.write_text("null\n[1]\n" + json.dumps(ROWS[1]) + "\n")
        self.assertEqual(cloud_digest.run(self.payload, self.state), 1)
        self.assertEqual(cloud_digest.run(self.payload, self.state), 0)

    def test_a_rewritten_larger_transcript_starts_over(self):
        write(self.transcript, ROWS[:2])
        cloud_digest.run(self.payload, self.state)
        write(self.transcript, [user("u9", "2026-11-20T11:00:00Z", "different history")] + ROWS[1:4] * 3)
        self.assertGreater(cloud_digest.run(self.payload, self.state), 0)

    def test_subagent_transcripts_are_shipped_in_the_same_layout(self):
        write(self.transcript, ROWS[:2])
        sub = self.tmp / "s1" / "subagents"
        sub.mkdir(parents=True)
        write(sub / "agent-x.jsonl", [assistant("s1", "2026-11-20T10:00:07Z", "reqsub")])
        self.assertEqual(cloud_digest.run(self.payload, self.state), 3)
        root = self.state / "projects" / self.tmp.name
        self.assertTrue((root / "s1.jsonl").exists())
        self.assertTrue((root / "s1" / "subagents" / "agent-x.jsonl").exists())

    def test_redirects_and_plain_http_are_refused(self):
        import http.server
        import os
        import threading

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))  # drain the body so the client sees the reply on every platform
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.handle_request, daemon=True).start()
        self.assertFalse(cloud_digest._post(f"http://127.0.0.1:{server.server_port}/ingest", "t", b"{}\n"))
        self.assertFalse(cloud_digest._post("http://example.com/ingest", "t", b"{}\n"))  # a bearer token is never sent in clear text
        server.server_close()

    def test_failures_never_raise(self):
        self.assertEqual(cloud_digest.run({"transcript_path": str(self.tmp / "missing.jsonl")}, self.state), 0)
        write(self.transcript, ROWS)
        import os
        os.environ["TOKENATLAS_CLOUD_URL"] = "http://127.0.0.1:9/ingest"  # nothing listens
        try:
            self.assertEqual(cloud_digest.run(self.payload, self.state), 3)
        finally:
            del os.environ["TOKENATLAS_CLOUD_URL"]
        self.assertTrue((self.state / "s1.pending.jsonl").read_text())  # stays queued for the next Stop


WORKER_ERROR = {"error": {"code": "refused", "message": "x"}}


class FakeIngest:
    """A local HTTP server answering from a script: each item is (status, body dict | None, headers)."""

    def __init__(self, script):
        import http.server
        import threading
        self.script, self.requests = list(script), []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((dict(self.headers), body))
                status, payload, headers = outer.script.pop(0) if outer.script else (200, "ok", {})
                if payload == "ok":
                    lines = [l for l in body.splitlines() if l]
                    payload = {"results": [{"status": "accepted"} for _ in lines]}
                out = json.dumps(payload or {}).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/ingest"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CloudSendTest(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.proj = self.tmp / "p"
        self.proj.mkdir()
        self.transcript = self.proj / "s1.jsonl"
        write(self.transcript, ROWS)
        self.payload = {"transcript_path": str(self.transcript), "session_id": "s1", "hook_event_name": "Stop"}
        os.environ["TOKENATLAS_CATCHUP_SECONDS"] = "0"
        os.environ["TOKENATLAS_CLOUD_TOKEN"] = "tka_x_y_z"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("TOKENATLAS_CATCHUP_SECONDS", "TOKENATLAS_CLOUD_TOKEN", "TOKENATLAS_CLOUD_URL")])
        self.server = None

    def serve(self, script):
        import os
        self.server = FakeIngest(script)
        self.addCleanup(self.server.close)
        os.environ["TOKENATLAS_CLOUD_URL"] = self.server.url

    def queue(self):
        p = self.state / "s1.pending.jsonl"
        return [l for l in p.read_text().splitlines() if l] if p.exists() else []

    def send_state(self):
        return json.loads((self.state / "send-state.json").read_text())

    def test_accepted_rows_leave_the_queue_and_carry_session_and_path(self):
        self.serve([(200, "ok", {})])
        self.assertEqual(cloud_digest.run(self.payload, self.state), 3)
        self.assertEqual(self.queue(), [])
        headers, body = self.server.requests[0]
        self.assertEqual({k.lower(): v for k, v in headers.items()}["x-tokenatlas-client"], "2")
        row = json.loads(body.splitlines()[0])
        self.assertEqual((row["session"], row["_path"]), ("s1", ""))

    def test_per_line_receipts_keep_only_retryable_rows(self):
        results = [{"status": "accepted"}, {"status": "rejected", "code": "bad"}, {"status": "quota"}]
        self.serve([(200, {"results": results}, {})])
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.queue()), 1)  # quota stays, rejected is quarantined, accepted is gone
        self.assertEqual(self.send_state()["quarantined"], 1)
        self.assertIn('"rejected"', (self.state / "quarantine.jsonl").read_text())
        self.assertGreater(self.send_state()["backoff_until"], time.time())

    def test_a_short_receipt_keeps_the_whole_batch(self):
        self.serve([(200, {"results": [{"status": "accepted"}]}, {})])
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.queue()), 3)

    def test_413_halves_the_batch_until_it_fits(self):
        self.serve([(413, {}, {}), (200, "ok", {}), (200, "ok", {})])
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(self.queue(), [])
        self.assertEqual([len(b.splitlines()) for _, b in self.server.requests], [3, 1, 1, 1])

    def test_401_turns_sending_off_until_the_token_changes(self):
        import os
        self.serve([(401, WORKER_ERROR, {})])
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.queue()), 3)
        self.assertTrue((self.state / "WARNING.txt").exists())
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.server.requests), 1)  # no second attempt
        os.environ["TOKENATLAS_CLOUD_TOKEN"] = "tka_x_y_other"
        self.server.script.append((200, "ok", {}))
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(self.queue(), [])

    def test_426_turns_sending_off_and_keeps_the_queue(self):
        self.serve([(426, WORKER_ERROR, {})])
        cloud_digest.run(self.payload, self.state)
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.queue()), 3)

    def test_429_honours_retry_after(self):
        self.serve([(429, {}, {"Retry-After": "120"})])
        cloud_digest.run(self.payload, self.state)
        wait = self.send_state()["backoff_until"] - time.time()
        self.assertTrue(100 < wait <= 120)
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.server.requests), 1)  # still backing off
        self.assertEqual(len(self.queue()), 3)

    def test_an_oversized_row_is_quarantined_without_blocking_the_rest(self):
        self.serve([(200, "ok", {})])
        write(self.transcript, ROWS[:2])
        cloud_digest.run({**self.payload, "hook_event_name": "SessionStart"}, self.state)  # digest + send once
        self.serve([(200, "ok", {})])
        pend = self.state / "s1.pending.jsonl"
        pend.write_bytes(b'{"x":"' + b"a" * (cloud_digest.MAX_ROW + 10) + b'"}\n' + b'{"ok":1}\n')
        cloud_digest._send_queue(pend, "s1", self.server.url, "tka_x_y_z", self.state, time.monotonic() + 4)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.send_state()["quarantined"], 1)

    def test_workflow_subagents_are_found(self):
        wf = self.proj / "s1" / "subagents" / "workflows" / "wf_abc" 
        wf.mkdir(parents=True)
        write(wf / "agent-w1.jsonl", [assistant("w1", "2026-11-20T10:00:08Z", "reqw")])
        self.serve([(200, "ok", {})])
        self.assertEqual(cloud_digest.run(self.payload, self.state), 4)
        paths = {json.loads(l)["_path"] for _, b in self.server.requests for l in b.splitlines()}
        self.assertIn("subagents/workflows/wf_abc/agent-w1.jsonl", paths)

    def test_iteration_keeps_type_and_model(self):
        it = cloud_digest._iteration({"type": "message", "model": "claude-x", "input_tokens": 3, "text": "SECRET"})
        self.assertEqual(it.get("type"), "message")
        self.assertEqual(it.get("model"), "claude-x")
        self.assertNotIn("text", it)

    def test_session_start_flushes_queues_other_sessions_left(self):
        self.state.mkdir()
        (self.state / "old.pending.jsonl").write_text('{"uuid":"o1","session":"old","_path":""}\n')
        self.serve([(200, "ok", {})])
        cloud_digest.run({"transcript_path": str(self.transcript), "session_id": "s1", "hook_event_name": "SessionStart"}, self.state)
        self.assertEqual((self.state / "old.pending.jsonl").read_text(), "")

    def test_an_edge_refusal_without_the_worker_error_body_only_backs_off(self):
        self.serve([(403, None, {})])
        cloud_digest.run(self.payload, self.state)
        self.assertNotIn("disabled_for", self.send_state())
        self.assertGreater(self.send_state()["backoff_until"], time.time())

    def test_session_end_tries_even_during_back_off(self):
        self.serve([(503, None, {}), (200, "ok", {})])
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.queue()), 3)
        cloud_digest.run({**self.payload, "hook_event_name": "SessionEnd"}, self.state)
        self.assertEqual(self.queue(), [])

    def test_a_garbage_response_is_a_failure_not_an_exception(self):
        import socket
        import threading
        import os
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def answer():
            conn, _ = srv.accept()
            conn.recv(65536)
            conn.sendall(b"garbage\r\n\r\n")
            conn.close()

        threading.Thread(target=answer, daemon=True).start()
        self.addCleanup(srv.close)
        os.environ["TOKENATLAS_CLOUD_URL"] = f"http://127.0.0.1:{srv.getsockname()[1]}/v1/ingest"
        cloud_digest.run(self.payload, self.state)
        self.assertEqual(len(self.queue()), 3)
        self.assertGreater(self.send_state()["backoff_until"], time.time())

    def test_metadata_the_receiver_rejects_is_dropped_not_the_row(self):
        row = assistant("a9", "2026-11-20T10:00:05Z", "req9")
        row["cwd"] = "/x" * 300
        row["message"] = {**row["message"], "model": "<synthetic>"}
        out = cloud_digest.digest_row(row)
        self.assertNotIn("cwd", out)
        self.assertNotIn("model", out["message"])

    def test_send_state_merges_instead_of_overwriting(self):
        self.state.mkdir()
        before = {}
        mine = {"backoff_until": 5.0, "queued": {"a": 1}}
        cloud_digest._save_send_state(self.state, {"disabled_for": "tag", "queued": {"b": 2}}, before, "b")
        cloud_digest._save_send_state(self.state, mine, before, "a")
        merged = self.send_state()
        self.assertEqual((merged["disabled_for"], merged["backoff_until"], merged["queued"]), ("tag", 5.0, {"a": 1, "b": 2}))


if __name__ == "__main__":
    unittest.main()
