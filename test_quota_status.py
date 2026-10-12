import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tokenatlas.quota_status import collect, redact
from tokenatlas.report import build_report


UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def reading(identity, ts, *, harness="codex", limit_id=None, plan=None, windows=None, status="allowed"):
    return {
        "id": identity, "ts": ts, "harness": harness,
        "quota": {"status": status, "limit_id": limit_id, "plan_type": plan,
                   "windows": windows or []},
    }


def window(minutes, used, reset="2026-10-13T00:00:00+00:00", slot=None):
    value = {"minutes": minutes, "used_percent": used, "resets_at": reset}
    if slot is not None:
        value["slot"] = slot
    return value


class QuotaStatusTests(unittest.TestCase):
    def test_latest_raw_reading_is_account_wide_and_classified_by_minutes(self):
        rows = [
            reading("last", "2026-10-12T11:00:00+00:00", windows=[window(10080, 60, slot="primary")]),
            reading("older", "2026-10-12T10:00:00+00:00", windows=[window(300, 90, slot="secondary")]),
            reading("partial", "2026-10-12T11:30:00+00:00", windows=[window(300, 20, slot="secondary")]),
            reading("future", "2026-10-12T13:00:00+00:00", windows=[window(300, 99), window(10080, 99)]),
        ]
        got = collect(rows, now=NOW)
        self.assertEqual({(r["minutes"], r["used_percent"]) for r in got}, {(300, 20), (10080, 60)})
        self.assertEqual({r["observed_at"] for r in got}, {"2026-10-12T11:00:00+00:00", "2026-10-12T11:30:00+00:00"})

    def test_plans_limits_and_rejected_rows_stay_separate_at_one_timestamp(self):
        timestamp = "2026-10-12T11:00:00+00:00"
        rows = [
            reading("default", timestamp, status="rejected", windows=[window(300, 10)]),
            reading("pro", timestamp, limit_id="model-a", plan="pro", windows=[window(300, 20)]),
            reading("team", timestamp, limit_id="model-a", plan="team", windows=[window(300, 30)]),
            reading("other", timestamp, limit_id="model-b", plan="pro", windows=[window(300, 40)]),
        ]
        got = collect(rows, now=NOW)
        self.assertEqual({(r["account"], r["plan_type"], r["used_percent"]) for r in got},
                         {(None, None, 10), ("model-a", "pro", 20), ("model-a", "team", 30), ("model-b", "pro", 40)})

    def test_bad_fields_do_not_become_zero_and_missing_reset_is_allowed(self):
        rows = [reading("bad", "2026-10-12T11:00:00+00:00", windows=[
            window(300, True, None), window(10080, -1, None),
            window(300, 101, None), window(10080, float("inf"), None),
            window(42, 3),
        ])]
        got = collect(rows, now=NOW)
        self.assertEqual([(r["minutes"], r["used_percent"], r["resets_at"]) for r in got],
                         [(300, None, None), (10080, None, None)])

    def test_missing_reset_then_valid_later_reset_uses_latest_raw_value(self):
        rows = [
            reading("missing", "2026-10-12T10:00:00+00:00", windows=[window(300, 15, None)]),
            reading("valid", "2026-10-12T11:00:00+00:00", windows=[window(300, 25, "2026-10-13T00:00:00Z")]),
        ]
        self.assertEqual(collect(rows, now=NOW), [dict(harness="codex", account=None, plan_type=None, minutes=300,
                                                         used_percent=25, resets_at="2026-10-13T00:00:00+00:00",
                                                         observed_at="2026-10-12T11:00:00+00:00")])

    def test_quota_only_events_and_explicit_claude_windows_are_included(self):
        events = [reading("event", "2026-10-12T11:30:00+00:00", harness="codex",
                           limit_id="model-limit", plan="team", windows=[window(300, 33, None)], status="event")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude-quota.jsonl"
            path.write_text("\n".join([
                json.dumps({"ts": "2026-10-12T10:00:00+00:00", "five_hour": {"used_percent": 12, "resets_at": None}}),
                json.dumps({"ts": "2026-10-12T11:45:00+00:00", "seven_day": {"used_percent": 44, "resets_at": "2026-10-19T00:00:00Z"}}),
                "not json",
                json.dumps({"five_hour": {"used_percent": 88}}),
                json.dumps({"ts": "2026-10-12T11:58:00+00:00"}),
                json.dumps({"ts": "2026-10-12T11:59:00+00:00", "other": {"used_percent": 99}}),
            ]), encoding="utf-8")
            got = collect([], events, claude=path, now=NOW)
        by_key = {(r["harness"], r["account"], r["plan_type"], r["minutes"]): r for r in got}
        self.assertEqual(by_key[("codex", "model-limit", "team", 300)]["used_percent"], 33)
        self.assertEqual(by_key[("claude", None, None, 300)]["used_percent"], 12)
        self.assertEqual(by_key[("claude", None, None, 10080)]["used_percent"], 44)

    def test_report_uses_universe_and_redacts_scopes_but_quota_opt_out_omits_payload(self):
        older = reading("older", "2026-10-12T10:00:00+00:00", limit_id="private-limit", plan="mystery",
                         windows=[window(300, 20)])
        newer = reading("newer", "2026-10-12T11:00:00+00:00", limit_id="private-limit", plan="mystery",
                         windows=[window(300, 30)])
        known = reading("known", "2026-10-12T11:00:00+00:00", limit_id="known-limit", plan="pro",
                        windows=[window(300, 40)])
        # Add the minimum history fields required by build_report.
        for item in (older, newer, known):
            item.update(provider="openai", model="gpt", project_id=None, project_label=None,
                        session="s", parent_session=None, turn_id=item["id"], turn_confidence="observed",
                        effort=None, thread_kind="main", agent="a", origin="cli", tokens={
                            "fresh_input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0},
                        complete=True, id_synthetic=False, warnings=[], raw_usage={}, machine="m")
        payload = build_report([older], {}, redact=True, now=NOW, universe=[older, newer, known])
        scopes = {(row["account"], row["plan_type"]): row["used_percent"] for row in payload["quota_status"]}
        self.assertIn("pro", [plan for _, plan in scopes])
        self.assertEqual(sorted(scopes.values()), [30, 40])
        encoded_status = json.dumps(payload["quota_status"])
        self.assertNotIn("private-limit", encoded_status)
        self.assertNotIn("known-limit", encoded_status)
        self.assertNotIn("mystery", encoded_status)
        private = build_report([older], {}, redact=False, now=NOW, universe=[older, newer, known])
        private_scopes = {(row["account"], row["plan_type"]): row["used_percent"] for row in private["quota_status"]}
        self.assertEqual(private_scopes[("private-limit", "mystery")], 30)
        self.assertEqual(private_scopes[("known-limit", "pro")], 40)
        self.assertNotIn("quota_status", build_report([newer], {}, redact=True, now=NOW, quota=False))

    def test_known_plan_is_public_unknown_plan_is_aliased_and_shared_requires_callbacks(self):
        row = dict(harness="codex", account="limit", plan_type="pro", minutes=300,
                   used_percent=1, resets_at=None, observed_at=NOW.isoformat())
        self.assertEqual(collect([], now=NOW), [])
        self.assertEqual(redact([row], shared=True, account_alias=lambda value: "limit 001",
                                plan_alias=lambda value: value if value == "pro" else "plan 001")[0]["plan_type"], "pro")
        with self.assertRaises(ValueError):
            redact([row], shared=True)

    def test_events_only_report_and_quota_opt_out_do_not_read_sidecar(self):
        event = reading("event", "2026-10-12T11:00:00+00:00", status="event", windows=[window(300, 42)])
        with mock.patch("tokenatlas.report.quota_status.collect", side_effect=AssertionError("sidecar read")):
            payload = build_report([], {}, now=NOW, quota=False, claude_quota="/must/not/read")
        self.assertNotIn("quota_status", payload)
        payload = build_report([], {}, now=NOW, quota=True, quota_events=[event])
        self.assertEqual(payload["quota_status"][0]["used_percent"], 42)


if __name__ == "__main__":
    unittest.main()

