import json
import unittest
from datetime import datetime, timezone

from test_report import decode_html, expand, observation
from tokenatlas import subscriptions
from tokenatlas.report import REDACTION_REVISION, build_report, render_report


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def quota(plan, used=20):
    return {"plan_type": plan, "windows": [{"minutes": 300, "used_percent": used,
                                               "resets_at": "2026-10-12T14:00:00+00:00"}]}


class ReportPlans(unittest.TestCase):
    def test_annotation_inherits_nearest_snapshot_over_whole_history(self):
        source = [
            observation("before", ts="2026-10-10T10:00:00+00:00", harness="codex", session="same", quota=quota("business")),
            observation("after", ts="2026-10-11T10:00:00+00:00", harness="codex", session="same", quota=quota(None)),
        ]
        self.assertEqual([r["plan"] for r in subscriptions.annotate(source)], ["business", "business"])

        selected = build_report([source[1]], {}, universe=source, redact=False, now=NOW)
        self.assertEqual(expand(selected)[0]["plan"], "business")
        self.assertEqual({r['plan_type'] for r in selected['quota_status']}, {'business'})
        self.assertEqual([(p['plan'], p['rows']) for p in selected['usage']['plans']], [('business', [0])])

    def test_rows_and_facts_are_additive_and_filter_safe(self):
        rows = [
            observation("b", ts="2026-10-10T10:00:00+00:00", harness="codex", session="b", quota=quota("business"),
                        tokens={"fresh_input": 1000, "cache_read": 0, "cache_write": 0, "output": 100, "reasoning": 0}),
            observation("p", ts="2026-10-11T10:00:00+00:00", harness="codex", session="p", quota=quota("pro"),
                        tokens={"fresh_input": 2000, "cache_read": 0, "cache_write": 0, "output": 200, "reasoning": 0}),
        ]
        report = build_report(rows[:1], {}, universe=rows, redact=False, now=NOW)
        self.assertEqual(expand(report)[0]["plan"], "business")
        self.assertEqual({p["plan"] for p in report["insights"]["plans"]}, {"business"})
        self.assertEqual(sum(p["observations"] for p in report["insights"]["plans"]), 1)
        self.assertEqual(REDACTION_REVISION, 5)

    def test_shared_unknown_plan_is_aliased_and_public_plan_survives(self):
        rows = [
            observation("public", ts="2026-10-10T10:00:00+00:00", harness="codex", session="public", quota=quota("pro")),
            observation("private", ts="2026-10-11T10:00:00+00:00", harness="codex", session="private", quota=quota("acme-internal")),
        ]
        report = build_report(rows, {}, redact=True, now=NOW)
        payload = json.dumps(report)
        self.assertIn('"plan": "pro"', payload)
        self.assertNotIn("acme-internal", payload)
        self.assertTrue(any(p["plan"].startswith("plan ") for p in report["insights"]["plans"]))

    def test_quota_windows_carry_plan(self):
        rows = [observation("one", harness="codex", session="quota", quota=quota("pro"))]
        report = build_report(rows, {}, redact=False, now=NOW)
        self.assertEqual(report["quota_windows"][0]["plan"], "pro")
        self.assertIn("plan", decode_html(render_report(report))["columns"]["dict"])


if __name__ == "__main__":
    unittest.main()
