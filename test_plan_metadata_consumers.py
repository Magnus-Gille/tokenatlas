import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tokenatlas import budget, limits, quota_share, quota_status, subscriptions


UTC = timezone.utc
START = datetime(2026, 10, 1, 12, tzinfo=UTC)
RESET = (START + timedelta(hours=5)).isoformat()


def quota(plan=None, used=99, status="event", reached=None):
    return {
        "status": status,
        "limit_id": "codex",
        "plan_type": plan,
        "reached": reached,
        "windows": [{"minutes": 300, "used_percent": used, "resets_at": RESET}],
    }


def record(identity, at, q, *, plan=None, turn="t1"):
    value = {
        "id": identity,
        "ts": at.isoformat(),
        "harness": "codex",
        "provider": "openai",
        "machine": "m",
        "session": "s1",
        "parent_session": None,
        "turn_id": turn,
        "thread_kind": "main",
        "quota": q,
        "complete": True,
        "id_synthetic": False,
    }
    if plan is not None:
        value["plan"] = plan
    return value


class PlanMetadataConsumers(unittest.TestCase):
    def test_inherited_plan_is_shared_by_request_event_hit_and_budget(self):
        anchor = record("anchor", START - timedelta(minutes=30), quota("pro"), turn="anchor")
        request = record("request", START, quota(None, used=100, reached="window_full"))
        event = record("event", START + timedelta(minutes=1), quota(None, used=100, reached="window_full"))
        annotated_requests, annotated_events = subscriptions.annotate_with_events([anchor, request], [event])
        request, event = annotated_requests[1], annotated_events[0]

        assigned = [("codex", "s1", "t1")]
        snapshots = quota_share.snapshots_from_records([request], assigned=assigned, events=[event])
        self.assertEqual({s["plan_type"] for s in snapshots}, {"pro"})
        self.assertEqual({s["plan_type"] for s in snapshots.events}, {"pro"})
        with patch.object(quota_share, "turn_shares", return_value={}):
            computed, _ = quota_share.compute([request], {}, assigned=assigned, events=[event])
        self.assertEqual({s["plan_type"] for s in computed.events}, {"pro"})

        hits = limits.limit_hits([request], [event], table={}, assigned=assigned)
        self.assertTrue(hits)
        self.assertEqual({hit["origin"]["plan_type"] for hit in hits}, {"pro"})
        self.assertEqual(budget.plan_of(request), "pro")
        self.assertEqual(budget.turn_costs([request], assigned, lambda row: 0.05),
                         {('codex', 's1', 't1'): (0.05, False, 'pro')})
        self.assertEqual(quota_status.collect([request], [event], now=(START + timedelta(hours=1)).isoformat()), [
            {
                "harness": "codex", "account": None, "plan_type": "pro", "minutes": 300,
                "used_percent": 100, "resets_at": RESET,
                "observed_at": (START + timedelta(minutes=1)).isoformat(),
            }
        ])

        key = next(iter(snapshots))["key"]
        snapshot = next(iter(snapshots))
        self.assertIs(quota_share._table_hit(hits, key, [snapshot], 100), True)
        self.assertIsNone(request["quota"]["plan_type"])
        self.assertIsNone(event["quota"]["plan_type"])

    def test_direct_quota_plan_wins_over_supplied_annotation(self):
        source = record("direct", START, quota("team"), plan="pro")
        annotated = subscriptions.annotate_with_events([source])[0][0]
        self.assertEqual(quota_share.snapshots_from_records([annotated])[0]["plan_type"], "team")
        self.assertEqual(budget.plan_of(annotated), "team")
        self.assertEqual(limits._scope(annotated)["origin"]["plan_type"], "team")
        self.assertEqual(quota_status.collect([annotated], now=(START + timedelta(hours=1)).isoformat())[0]["plan_type"], "team")
        self.assertEqual(annotated["quota"]["plan_type"], "team")

    def test_event_anchor_infers_request_plan_without_crossing_sessions(self):
        event = record('anchor', START, quota(' PRO '), turn=None)
        request = record('request', START + timedelta(minutes=1), quota(None))
        other = dict(request, id='other', session='different')
        requests, events = subscriptions.annotate_with_events([request, other], [event])
        self.assertEqual([row['plan'] for row in requests], ['pro', 'unknown'])
        self.assertEqual(events[0]['plan'], 'pro')
        self.assertNotIn('plan', event)
        self.assertNotIn('plan', request)
        self.assertEqual(events[0]['quota']['plan_type'], ' PRO ')

    def test_limit_episodes_keep_different_canonical_plans_separate(self):
        first = record("first", START, quota(None, used=100, reached="window_full"), plan="pro")
        second = record("second", START + timedelta(minutes=1), quota(None, used=100, reached="window_full"), plan="team")
        hits = limits.limit_hits([first, second], [], table={}, assigned=[("codex", "s1", "t1"), ("codex", "s1", "t2")])
        self.assertEqual({hit["origin"]["plan_type"] for hit in hits}, {"pro", "team"})
        self.assertEqual(len(hits), 2)


if __name__ == "__main__":
    unittest.main()
