import base64
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tokenatlas import cloud_sync

FIXTURE = Path(__file__).parent / "cloud-ingest" / "fixtures" / "digest-valid.ndjson"


def cursor(seq, generation=1):
    raw = json.dumps({"t": "t1", "g": generation, "s": seq}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def envelopes():
    out = []
    for i, line in enumerate(FIXTURE.read_text().splitlines(), start=1):
        wire = json.loads(line)
        session, path = wire.pop("session"), wire.pop("_path", "")
        key = hashlib.sha256(json.dumps(["t1", "claude", session, path, wire["uuid"]]).encode()).hexdigest()
        out.append({"seq": i * 2, "dedupe_key": key, "source": "claude", "token_id": "tk1", "session_id": session,
                    "rel_path": path, "received_at": f"2026-11-21T22:0{i}:00Z", "row": wire})
    return out


class Service:
    """Serves envelopes after a cursor like the real Worker: opaque cursor, ascending seq, 410 on a stale generation."""

    def __init__(self, events, generation=1, page=1000):
        self.events, self.generation, self.page, self.requests = events, generation, page, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                if self.headers["Authorization"] != "Bearer tka_r":
                    body = json.dumps({"error": {"code": "unauthorized", "message": "x"}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                query = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&"))
                after = 0
                if "after" in query:
                    c = json.loads(base64.urlsafe_b64decode(query["after"] + "=" * (-len(query["after"]) % 4)))
                    if c["g"] != outer.generation:
                        body = json.dumps({"error": {"code": "cursor_reset", "message": "x"}}).encode()
                        self.send_response(410)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    after = c["s"]
                rows = [e for e in outer.events if e["seq"] > after][: outer.page]
                last = rows[-1]["seq"] if rows else after
                body = "".join(json.dumps(e) + "\n" for e in rows).encode()
                self.send_response(200)
                self.send_header("X-Next-Cursor", cursor(last, outer.generation))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/events"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CloudSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "history.sqlite3"
        self.events = envelopes()
        env = {"TOKENATLAS_CLOUD_READ_TOKEN": "tka_r"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, **kw):
        self.service = Service(self.events, **kw)
        self.addCleanup(self.service.close)
        os.environ["TOKENATLAS_CLOUD_PULL_URL"] = self.service.url

    def test_clean_row_agrees_with_the_hook_on_every_fixture_row(self):
        for line in FIXTURE.read_text().splitlines():
            wire = json.loads(line)
            wire.pop("session"), wire.pop("_path", None)
            self.assertEqual(cloud_sync.clean_row(wire), wire)

    def test_untrusted_rows_are_reduced_and_bad_ones_refused(self):
        row = {"type": "assistant", "uuid": "u", "message": {"model": "m", "content": "SECRET", "usage": {"input_tokens": 5, "prompt": "SECRET", "iterations": "x"}}, "extra": "SECRET"}
        out = cloud_sync.clean_row(row)
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertEqual(out["message"]["usage"], {"input_tokens": 5})
        self.assertIsNone(cloud_sync.clean_row({"type": "assistant", "message": {}}))
        good = self.events[1]
        for patch in ({"session_id": "../x"}, {"rel_path": "../../etc/passwd"}, {"dedupe_key": "zz"}, {"seq": -1}, {"rel_path": "subagents/agent-a.jsonl/../x"}):
            self.assertIsNone(cloud_sync.clean_envelope(json.dumps({**good, **patch})), patch)
        self.assertIsNone(cloud_sync.clean_envelope("not json"))
        self.assertIsNotNone(cloud_sync.clean_envelope(json.dumps(good)))

    def test_pull_stores_once_builds_the_root_and_a_replay_adds_nothing(self):
        self.serve(page=2)
        first = cloud_sync.pull(self.db)
        self.assertEqual((first["events_new"], first["rejected"], first["events"]), (len(self.events), 0, len(self.events)))
        self.assertGreater(first["pages"], 2)  # paged
        main = Path(first["root"]) / "cloud" / f"{self.events[0]['session_id']}.jsonl"
        self.assertTrue(main.is_file())
        second = cloud_sync.pull(self.db)
        self.assertEqual(second["events_new"], 0)
        self.assertEqual(second["events_seen"], 0)  # resumed from the cursor
        status = cloud_sync.status(self.db)
        self.assertEqual(status["events"], len(self.events))
        self.assertTrue(status["last_pull_at"])

    def test_a_stale_cursor_repulls_from_zero_and_dedupe_absorbs_the_overlap(self):
        self.serve()
        cloud_sync.pull(self.db)
        self.service.generation = 2  # storage was reset
        again = cloud_sync.pull(self.db)
        self.assertTrue(again["reset"])
        self.assertEqual(again["events_new"], 0)
        self.assertEqual(again["events"], len(self.events))

    def test_rejected_envelopes_are_counted_and_not_stored(self):
        self.events.append({**self.events[0], "seq": 99, "dedupe_key": "f" * 64, "session_id": "bad/id"})
        self.serve()
        result = cloud_sync.pull(self.db)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["events"], len(self.events) - 1)

    def test_configuration_and_transport_are_checked(self):
        os.environ.pop("TOKENATLAS_CLOUD_PULL_URL", None)
        os.environ.pop("TOKENATLAS_CLOUD_URL", None)
        with self.assertRaises(cloud_sync.CloudSyncError):
            cloud_sync.pull(self.db)
        os.environ["TOKENATLAS_CLOUD_URL"] = "http://example.com/v1/ingest"
        with self.assertRaisesRegex(cloud_sync.CloudSyncError, "https"):
            cloud_sync.pull(self.db)
        os.environ["TOKENATLAS_CLOUD_URL"] = "https://ingest.example.com/v1/ingest"
        self.assertEqual(cloud_sync.endpoint()[0], "https://ingest.example.com/v1/events")

    def test_a_refused_token_is_an_actionable_error(self):
        os.environ["TOKENATLAS_CLOUD_READ_TOKEN"] = "tka_wrong"
        self.serve()
        with self.assertRaisesRegex(cloud_sync.CloudSyncError, "refused the read token"):
            cloud_sync.pull(self.db)

    def test_cli_imports_the_cloud_root_into_the_history(self):
        self.serve()
        env = {**os.environ, "TOKENATLAS_NO_BROWSER": "1"}
        run = lambda *a: subprocess.run([sys.executable, "-m", "tokenatlas", "--db", str(self.db), *a], capture_output=True, text=True, env=env,
                                        cwd=Path(__file__).parent)
        out = run("sync-cloud", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["schema"], "tokenatlas.sync-cloud/1")
        self.assertEqual(data["events_new"], len(self.events))
        top = run("report", "--harness", "claude", "--json")
        self.assertEqual(top.returncode, 0, top.stderr)
        totals = json.loads(top.stdout)["totals"]
        self.assertTrue(any(v for v in totals.values() if isinstance(v, (int, float))), totals)

    def test_refresh_all_cloud_reports_an_unreachable_service_without_failing(self):
        env = {**os.environ, "TOKENATLAS_CLOUD_PULL_URL": "http://127.0.0.1:9/v1/events", "HOME": str(self.tmp), "XDG_STATE_HOME": str(self.tmp / "s"),
               "TOKENATLAS_NO_BROWSER": "1"}
        out = subprocess.run([sys.executable, "-m", "tokenatlas", "--db", str(self.db), "refresh", "--all", "--cloud", "--json"],
                             capture_output=True, text=True, env=env, cwd=Path(__file__).parent)
        self.assertIn(out.returncode, (0, 3), out.stderr)
        self.assertEqual(json.loads(out.stdout)["cloud"]["status"], "error")

    def test_freshness_reaches_the_report_coverage_and_its_reuse_key(self):
        from tokenatlas.report import build_report, coverage_key
        self.serve()
        cloud_sync.pull(self.db)
        status = {"cloud": cloud_sync.status(self.db)}
        self.assertEqual(build_report([], status)["coverage"]["cloud"]["sessions"], status["cloud"]["sessions"])
        key = coverage_key(status)["cloud"]
        self.assertIn("last_pull_at", key)
        self.assertIsNone(coverage_key({})["cloud"])

    def test_an_interrupted_pull_still_builds_the_root_for_committed_pages(self):
        self.serve(page=2)
        real = cloud_sync._get
        calls = []

        def flaky(url, token):
            calls.append(url)
            if len(calls) == 2:
                raise cloud_sync.CloudSyncError("timeout")
            return real(url, token)

        with mock.patch.object(cloud_sync, "_get", flaky):
            with self.assertRaises(cloud_sync.CloudSyncError):
                cloud_sync.pull(self.db)
        files = list((cloud_sync.directory(self.db) / "projects" / "cloud").rglob("*.jsonl"))
        self.assertTrue(files)  # page 1 reached the root even though the pull failed
        # a deleted root is rebuilt from the stored events without any new row
        import shutil
        shutil.rmtree(cloud_sync.directory(self.db) / "projects")
        again = cloud_sync.pull(self.db)
        self.assertEqual(again["events_new"], len(self.events) - 2)
        self.assertGreaterEqual(len(list((cloud_sync.directory(self.db) / "projects" / "cloud").rglob("*.jsonl"))), len(files))

    def test_token_with_line_ending_is_cleaned_or_refused_without_echoing_it(self):
        self.serve()
        os.environ["TOKENATLAS_CLOUD_READ_TOKEN"] = "tka_r\r\n"
        self.assertEqual(cloud_sync.pull(self.db)["events_new"], len(self.events))
        os.environ["TOKENATLAS_CLOUD_READ_TOKEN"] = "tka r"
        with self.assertRaises(cloud_sync.CloudSyncError) as ctx:
            cloud_sync.endpoint()
        self.assertNotIn("tka", str(ctx.exception))

    def test_a_bad_retention_setting_does_not_break_anything(self):
        os.environ["TOKENATLAS_CLOUD_RETENTION_DAYS"] = "90d"
        self.assertEqual(cloud_sync._retention_days(), 90)
        os.environ["TOKENATLAS_CLOUD_RETENTION_DAYS"] = ""
        self.assertEqual(cloud_sync._retention_days(), 90)

    def test_cloud_cwd_becomes_a_label_that_never_looks_like_a_local_path(self):
        env, row = cloud_sync.clean_envelope(json.dumps(self.events[1]))
        self.assertTrue(row["cwd"].startswith("cloud:"))
        self.assertFalse(row["cwd"].startswith("/"))

    def test_a_damaged_store_hides_the_cloud_line_and_the_next_pull_reports_it(self):
        folder = cloud_sync.directory(self.db)
        folder.mkdir(parents=True)
        (folder / "events.sqlite3").write_bytes(b"not a database" * 100)
        self.assertIsNone(cloud_sync.status(self.db))
        self.serve()
        with self.assertRaisesRegex(cloud_sync.CloudSyncError, "delete"):
            cloud_sync.pull(self.db)

    def test_a_page_with_rows_but_no_cursor_is_an_error_not_a_loop(self):
        self.serve()
        real = cloud_sync._get
        status, headers, body = real(self.service.url + "?limit=1000", "tka_r")
        with mock.patch.object(cloud_sync, "_get", lambda url, token: (200, {}, body)):
            with self.assertRaisesRegex(cloud_sync.CloudSyncError, "cursor"):
                cloud_sync.pull(self.db)


class SessionGapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "history.sqlite3"
        self.c = cloud_sync._open(cloud_sync.directory(self.db))
        self.addCleanup(self.c.close)
        self.n = 0
        cloud_sync._set(self.c, "caught_up", 1)

    def add(self, session, kind, stop=None, received="2026-11-21T10:00:00Z", rel=""):
        self.n += 1
        row = {"type": kind, "uuid": f"u{self.n}", "message": {"stop_reason": stop} if stop else {}}
        with self.c:
            self.c.execute("INSERT INTO events (dedupe_key, generation, seq, session_id, rel_path, received_at, row) VALUES (?,?,?,?,?,?,?)",
                           (f"{self.n:064x}", 1, self.n, session, rel, received, json.dumps(row)))

    NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc).timestamp()  # a day after the rows below, far past the quiet window

    def test_a_session_that_ends_mid_turn_is_listed_and_a_finished_one_is_not(self):
        self.add("done", "user"); self.add("done", "assistant", "end_turn")
        self.add("cut-prompt", "assistant", "end_turn"); self.add("cut-prompt", "user")
        self.add("cut-tool", "user"); self.add("cut-tool", "assistant", "tool_use")
        self.assertEqual(sorted(cloud_sync.session_gaps(self.c, self.NOW)), ["cut-prompt", "cut-tool"])
        self.assertEqual(cloud_sync.freshness(self.c, self.NOW)["incomplete"], 2)

    def test_a_session_quiet_for_less_than_two_hours_may_still_be_running(self):
        self.add("live", "user", received="2026-11-22T08:30:00Z")
        self.assertEqual(cloud_sync.session_gaps(self.c, self.NOW), [])  # 10 minutes after the last row

    def test_only_the_main_transcript_decides_and_the_last_row_is_by_delivery_order(self):
        self.add("s", "user"); self.add("s", "assistant", "end_turn")
        self.add("s", "assistant", "tool_use", rel="subagents/agent-a.jsonl")  # a subagent ending mid-tool does not count
        self.assertEqual(cloud_sync.session_gaps(self.c, self.NOW), [])

    def test_nothing_is_counted_until_the_pull_has_caught_up_and_recent_ones_come_first(self):
        self.add("old", "user", received="2026-11-20T10:00:00Z"); self.add("new", "user", received="2026-11-21T10:00:00Z")
        self.assertEqual(cloud_sync.session_gaps(self.c, self.NOW), ["new", "old"])
        cloud_sync._set(self.c, "caught_up", 0)
        self.assertEqual(cloud_sync.session_gaps(self.c, self.NOW), [])

    def test_the_report_gets_the_count_but_never_the_ids(self):
        from tokenatlas.report import build_report
        self.add("secret-session", "user")
        cloud = cloud_sync.freshness(self.c, self.NOW)
        coverage = build_report([], {"cloud": cloud})["coverage"]["cloud"]
        self.assertEqual(coverage["incomplete"], 1)
        self.assertNotIn("secret-session", json.dumps(coverage))


if __name__ == "__main__":
    unittest.main()
