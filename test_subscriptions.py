import copy
import unittest
from unittest import mock

from tokenatlas import prompts, subscriptions
from tokenatlas.subscriptions import PUBLIC_PLANS, annotate, label, plan_facts


TABLE = {
    "schema": 1,
    "retrieved_on": "2026-09-01",
    "unit": "per_million_tokens",
    "provider_aliases": {},
    "local_providers": [],
    "models": [{
        "provider": "openai", "model": "gpt-test", "aliases": [], "currency": "USD",
        "input": 2, "cache_write_5m": None, "cache_write_1h": None, "cache_write": 2,
        "cache_read": 1, "output": 10, "free": False,
    }],
}


def row(identity, ts, *, session="s", harness="codex", plan=None, turn="t", model="gpt-test", fresh=1_000_000, output=0):
    quota = {"plan_type": plan} if plan is not None else {}
    return {
        "id": identity, "ts": ts, "harness": harness, "session": session, "turn_id": turn,
        "thread_kind": "main", "agent": "main", "parent_session": None,
        "provider": "openai", "model": model, "quota": quota, "tokens": {
            "fresh_input": fresh, "cache_write": 0, "cache_read": 0, "output": output, "reasoning": 0,
        }, "raw_usage": {}, "tariff": None,
    }


class AnnotationTests(unittest.TestCase):
    def test_already_annotated_mixed_unknown_rows_do_not_allocate_another_universe(self):
        rows = annotate([row('claude', '2026-01-01T00:00:00Z', harness='claude'),
                         row('codex', '2026-01-01T00:00:00Z', session='unknown')])
        with mock.patch.object(subscriptions, 'annotate', side_effect=AssertionError('redundant annotation')):
            requests, events = subscriptions.annotate_with_events(rows)
        self.assertEqual(requests, rows)
        self.assertIs(requests[0], rows[0])
        self.assertEqual(events, [])

    def test_direct_plan_beats_nearest_snapshot(self):
        records = [row("before", "2026-01-01T00:00:00Z", plan="plus"),
                   row("direct", "2026-01-02T00:00:00Z", plan=" PRO "),
                   row("after", "2026-01-03T00:00:00Z", plan="team"),
                   row("target", "2026-01-02T12:00:00Z")]
        self.assertEqual(annotate(records)[3]["plan"], "pro")

    def test_nearest_equal_distance_prefers_earlier_snapshot(self):
        records = [row("later", "2026-01-03T00:00:00Z", plan="team"),
                   row("earlier", "2026-01-01T00:00:00Z", plan="plus"),
                   row("target", "2026-01-02T00:00:00Z")]
        self.assertEqual(annotate(records)[2]["plan"], "plus")

    def test_unknown_timestamp_and_other_session_do_not_inherit(self):
        records = [row("known", "2026-01-01T00:00:00Z", plan="plus"),
                   row("wrong-session", "2026-01-02T00:00:00Z", session="other"),
                   row("bad-time", "not-a-time")]
        found = annotate(records)
        self.assertEqual([r["plan"] for r in found], ["plus", "unknown", "unknown"])

    def test_other_harness_requires_direct_plan_and_custom_name_stays_private(self):
        records = [row("claude-inherited", "2026-01-02T00:00:00Z", harness="claude"),
                   row("claude-direct", "2026-01-03T00:00:00Z", harness="claude", plan="Private Workspace")]
        found = annotate(records)
        self.assertEqual(found[0]["plan"], "unknown")
        self.assertEqual(found[1]["plan"], "private workspace")
        self.assertEqual(label(found[1]["plan"]), "private workspace")
        self.assertIn("pro", PUBLIC_PLANS)
        self.assertNotIn("private workspace", PUBLIC_PLANS)

    def test_annotation_does_not_mutate_nested_record_data(self):
        records = [row("a", "2026-01-01T00:00:00Z", plan="plus")]
        original = copy.deepcopy(records)
        found = annotate(records)
        self.assertEqual(records, original)
        self.assertIsNot(records[0], found[0])
        self.assertNotIn("plan", records[0])


class PlanFactsTests(unittest.TestCase):
    def test_costs_are_partitioned_and_mixed_turns_overlap(self):
        records = [row("a", "2026-01-01T00:00:00Z", plan="plus", turn="t1", output=0),
                   row("b", "2026-01-01T00:01:00Z", plan="team", turn="t1", output=1_000_000),
                   row("c", "2026-01-01T00:02:00Z", plan="plus", turn="t2", output=0),
                   row("d", "2026-01-01T00:03:00Z", plan="plus", turn="t3", model="unknown-model")]
        result = plan_facts(records, TABLE)
        by_plan = {item["plan"]: item for item in result["plans"]}
        self.assertEqual(by_plan["plus"], {"plan": "plus", "label": "Plus", "observations": 3,
                                           "turns": 3, "cost": 4.0, "priced": 2, "unpriced": 1, "lower_bound": True})
        self.assertEqual(by_plan["team"], {"plan": "team", "label": "Business", "observations": 1,
                                           "turns": 1, "cost": 12.0, "priced": 1, "unpriced": 0, "lower_bound": False})
        self.assertIn("overlap", result["note"])

    def test_supplied_assignments_are_used(self):
        records = [row("a", "2026-01-01T00:00:00Z", plan="plus", turn="t1")]
        assigned = [("codex", "s", "supplied", "own")]
        self.assertEqual(plan_facts(records, TABLE, assigned=assigned)["plans"][0]["turns"], 1)

    def test_filtered_preannotated_rows_keep_snapshot_plan_from_outside_filter(self):
        universe = [row("snapshot", "2026-01-01T00:00:00Z", plan="plus"),
                    row("later", "2026-01-03T00:00:00Z", turn="t2")]
        annotated = annotate(universe)
        result = plan_facts([annotated[1]], TABLE)
        self.assertEqual(result["plans"][0]["plan"], "plus")

    def test_preannotated_rows_skip_annotation_without_changing_facts(self):
        records = [row("plus", "2026-01-01T00:00:00Z", turn="plus", plan="plus"),
                   row("team", "2026-01-01T00:01:00Z", turn="team", plan="team")]
        assigned = [("codex", "s", "plus", "own"), ("codex", "s", "team", "own")]
        expected = {"plans": [
            {"plan": "plus", "label": "Plus", "observations": 1, "turns": 1, "cost": 2.0,
             "priced": 1, "unpriced": 0, "lower_bound": False},
            {"plan": "team", "label": "Business", "observations": 1, "turns": 1, "cost": 2.0,
             "priced": 1, "unpriced": 0, "lower_bound": False},
        ], "note": "A turn that contains requests under multiple plans is counted once in each plan bucket; plan turn counts may overlap."}
        calls = []
        original = subscriptions.annotate

        def tracked(source):
            calls.append(len(source))
            return original(source)

        with mock.patch.object(subscriptions, "annotate", side_effect=tracked):
            found = plan_facts(records, TABLE, assigned=assigned)
        self.assertEqual(found, expected)
        self.assertEqual(calls, [])

    def test_direct_quota_plan_overrides_stale_supplied_plan(self):
        record = row("stale", "2026-01-01T00:00:00Z", plan="pro")
        record["plan"] = "plus"
        result = plan_facts([record], TABLE, assigned=[("codex", "s", "t", "own")])
        self.assertEqual(result["plans"][0]["plan"], "pro")

    def test_mixed_unannotated_rows_still_inherit_and_preserve_supplied_plan(self):
        snapshot = row("snapshot", "2026-01-01T00:00:00Z", plan="plus", turn="snapshot")
        inherited = row("inherited", "2026-01-03T00:00:00Z", turn="inherited")
        supplied = row("supplied", "2026-01-04T00:00:00Z", turn="supplied")
        supplied["plan"] = "pro"
        records = [snapshot, inherited, supplied]
        assigned = [("codex", "s", r["turn_id"], "own") for r in records]
        result = plan_facts(records, TABLE, assigned=assigned)
        by_plan = {item["plan"]: item for item in result["plans"]}
        self.assertEqual(by_plan["plus"]["observations"], 2)
        self.assertEqual(by_plan["pro"]["observations"], 1)


    def test_ambiguous_identity_does_not_inflate_plan_totals(self):
        known = row("known", "2026-01-01T00:00:00Z", plan="pro")
        ambiguous = dict(known, id="ambiguous", id_synthetic=True)
        result = plan_facts([known, ambiguous], TABLE)
        self.assertEqual(result["plans"][0]["observations"], 1)
        self.assertEqual(result["plans"][0]["priced"], 1)

    def test_all_unpriced_cost_is_unknown(self):
        unknown = row("unpriced", "2026-01-01T00:00:00Z", plan="pro")
        unknown["model"] = "no-rate"
        result = plan_facts([unknown], TABLE)
        self.assertIsNone(result["plans"][0]["cost"])
        self.assertEqual(result["plans"][0]["unpriced"], 1)

    def test_incomplete_known_cost_is_marked_as_lower_bound_in_cli_and_json(self):
        from tokenatlas.__main__ import _render_plan_facts
        incomplete = dict(row('partial', '2026-01-01T00:00:00Z', plan='pro'), complete=False)
        facts = plan_facts([incomplete], TABLE)
        self.assertTrue(facts['plans'][0]['lower_bound'])
        self.assertIn('priced cost ≥$', _render_plan_facts(facts))


if __name__ == "__main__":
    unittest.main()
