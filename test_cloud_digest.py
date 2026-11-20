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
        self.payload = {"transcript_path": str(self.transcript), "session_id": "s1"}

    def spool(self):
        return (self.state / "s1.digest.jsonl").read_text()

    def test_digest_drops_text_and_keeps_usage(self):
        write(self.transcript, ROWS)
        self.assertEqual(cloud_digest.run(self.payload, self.state), 3)  # one prompt, two requests
        text = self.spool()
        for secret in ("SECRET", "iterations", "server_tool_use"):
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
            root = self.tmp / file.stem / "projects"
            (root / "-home-user-repo").mkdir(parents=True)
            (root / "-home-user-repo" / "s1.jsonl").write_bytes(file.read_bytes())
            return sorted((r.call_id, r.model, r.fresh_input, r.cache_read, r.cache_write, r.output, r.project)
                          for r in why.collect_claude(root, start, end))

        original, digest = read(self.transcript), read(self.state / "s1.digest.jsonl")
        self.assertEqual(len(original), 2)
        self.assertEqual(original, digest)

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
