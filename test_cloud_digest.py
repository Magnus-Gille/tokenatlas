import json
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
