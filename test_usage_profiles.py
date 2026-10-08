import json
import os
import tempfile
import unittest
from pathlib import Path

from tokenatlas import __main__, report, usage_profiles
from test_report import observation


class UsageProfileTests(unittest.TestCase):
    def test_profile_is_atomic_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.sqlite3"
            got = usage_profiles.save(db, {"plans": {"claude": "max-5x"}, "local_providers": ["my-local"]})
            path = usage_profiles.path_for(db)
            self.assertEqual(got["plans"]["claude"], "max-5x")
            self.assertEqual(usage_profiles.load(db), got)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["local_providers"], ["my-local"])

    def test_mutation_refuses_corrupt_existing_profile_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.sqlite3"
            path = usage_profiles.path_for(db)
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                usage_profiles.save(db, {"plans": {"claude": "max-5x"}})
            self.assertIsNone(usage_profiles._valid_name("bad\nname"))

    def test_unknown_origin_is_not_presented_as_cli(self):
        self.assertEqual(usage_profiles.client_key("claude", "new-origin"), "unknown")
        self.assertEqual(usage_profiles.client_key("codex", "new-origin"), "unknown")
        self.assertEqual(usage_profiles.client_key("claude", "local-agent"), "local_agent")

    def test_cli_plan_and_local_provider_commands_use_only_profile_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.sqlite3"
            self.assertEqual(__main__.main(["--db", str(db), "plan", "set", "--harness", "claude", "max-5x"]), 0)
            self.assertEqual(__main__.main(["--db", str(db), "profile", "provider", "add", "ollama-local"]), 0)
            data = usage_profiles.load(db)
            self.assertEqual(data["plans"]["claude"], "max-5x")
            self.assertIn("ollama-local", data["local_providers"])
            self.assertEqual(__main__.main(["--db", str(db), "plan", "remove", "--harness", "claude"]), 0)
            self.assertNotIn("claude", usage_profiles.load(db)["plans"])

    def test_usage_payload_tracks_clients_local_rows_and_only_observed_codex_plans(self):
        rows = [
            observation("claude", harness="claude", origin="claude-desktop", provider="anthropic"),
            observation("codex", harness="codex", origin="codex_exec", provider="openai",
                        quota={"plan_type": "pro", "windows": []}),
            observation("local", harness="pi", origin="cli", provider="ollama"),
        ]
        payload = report.build_report(rows, {}, redact=False, profile={"plans": {}, "local_providers": []})
        self.assertEqual(payload["usage"]["source_keys"], ["claude_desktop", "codex_exec", "pi"])
        self.assertEqual(payload["usage"]["local_rows"], [2])
        self.assertEqual(payload["usage"]["plans"][0]["plan"], "pro")
        self.assertEqual(payload["usage"]["plans"][0]["rows"], [1])

    def test_missing_plans_are_explicit_and_follow_only_their_rows(self):
        rows = [observation('claude', harness='claude'), observation('codex', harness='codex'), observation('codex-pro', harness='codex', quota={'plan_type':'pro', 'windows':[]})]
        payload = report.build_report(rows, {}, redact=False)
        unknown = {p['harness']:p['rows'] for p in payload['usage']['plans'] if p['source']=='unknown'}
        self.assertEqual(unknown, {'claude':[0], 'codex':[1]})
        manual = report.build_report(rows, {}, redact=False, profile={'plans':{'claude':'max-5x'}})
        self.assertFalse(any(p['harness']=='claude' and p['source']=='unknown' for p in manual['usage']['plans']))

    def test_private_work_context_uses_retained_text_and_shared_report_omits_it(self):
        row = observation("work", project_id="/private/secret-repo", project_label="secret-repo")
        key = (row["harness"], row["session"], row["turn_id"])
        context = {key: {"title": "Sensitive session title", "repository": "https://example.test/private.git", "branch": "feature/secret"}}
        private = report.build_report([row], {}, redact=False, prompt_context=context, prompt_texts={key: "Retained prompt"})
        self.assertEqual(private["usage"]["groups"][0]["branch"], "feature/secret")
        self.assertEqual(private["usage"]["groups"][0]["turns"][0]["title"], "Sensitive session title")
        shared = report.build_report([row], {}, redact=True)
        self.assertNotIn("Sensitive session title", json.dumps(shared))
        self.assertEqual(shared["usage"]["groups"], [])


if __name__ == "__main__":
    unittest.main()
