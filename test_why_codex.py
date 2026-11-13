#!/usr/bin/env python3
"""Regression tests for Codex per-turn attribution."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why


def _write_rollout(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _meta(session_id="session-1", source="cli", originator="codex-tui"):
    return {
        "timestamp": "2026-09-03T08:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": session_id, "model_provider": "openai", "source": source,
            "originator": originator, "cwd": "/work/initial",
        },
    }


def _context(timestamp, model, effort, cwd):
    return {
        "timestamp": timestamp, "type": "turn_context",
        "payload": {"model": model, "effort": effort, "cwd": cwd},
    }


def _tokens(timestamp, ordinal, last, cumulative=None):
    return {
        "timestamp": timestamp, "ordinal": ordinal, "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": cumulative or {
                    "input_tokens": 999999, "output_tokens": 999999,
                },
            },
        },
    }


class CodexAttributionTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def test_uses_per_turn_deltas_and_normalizes_token_semantics(self):
        rows = [
            _meta(),
            _context("2026-09-03T09:00:00Z", "gpt-test", "high", "/work/project"),
            _tokens("2026-09-03T09:01:00Z", 10, {
                "input_tokens": 1000, "cached_input_tokens": 600,
                "cache_write_input_tokens": 100, "output_tokens": 80,
                "reasoning_output_tokens": 50,
            }, {
                "input_tokens": 1000, "cached_input_tokens": 600,
                "cache_write_input_tokens": 100, "output_tokens": 80,
                "reasoning_output_tokens": 50,
            }),
            _tokens("2026-09-03T09:02:00Z", 11, {
                "input_tokens": 200, "cached_input_tokens": 50,
                "cache_write_input_tokens": 25, "output_tokens": 20,
                "reasoning_output_tokens": 10,
            }, {
                "input_tokens": 1200, "cached_input_tokens": 650,
                "cache_write_input_tokens": 125, "output_tokens": 100,
                "reasoning_output_tokens": 60,
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-one.jsonl", rows)
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual(len(records), 2)
        self.assertEqual(sum(row.fresh_input for row in records), 425)
        self.assertEqual(sum(row.cache_read for row in records), 650)
        self.assertEqual(sum(row.cache_write for row in records), 125)
        self.assertEqual(sum(row.output for row in records), 100)
        self.assertEqual(sum(row.reasoning for row in records), 60)
        self.assertEqual(sum(row.total_tokens for row in records), 1300)

    def test_per_turn_deltas_reconcile_with_final_cumulative_total(self):
        first = {
            "input_tokens": 100, "cached_input_tokens": 50,
            "cache_write_input_tokens": 10, "output_tokens": 20,
            "reasoning_output_tokens": 5,
        }
        second = {
            "input_tokens": 200, "cached_input_tokens": 75,
            "cache_write_input_tokens": 25, "output_tokens": 40,
            "reasoning_output_tokens": 15,
        }
        final = {field: first[field] + second[field] for field in first}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-reconcile.jsonl"
            _write_rollout(path, [
                _meta(),
                _tokens("2026-09-03T09:01:00Z", 1, first, first),
                # Codex may emit the same token snapshot again during a later
                # UI/status event. This is not another model call.
                _tokens("2026-09-03T09:01:01Z", 2, first, first),
                _tokens("2026-09-03T09:02:00Z", 3, second, final),
            ])
            result = why.reconcile_codex_rollout(path)

        self.assertTrue(result["matches"])
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["summed"], result["final"])

    def test_reconciliation_accounts_for_cumulative_counter_reset(self):
        first = {"input_tokens": 100, "output_tokens": 10}
        second = {"input_tokens": 50, "output_tokens": 5}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-reset.jsonl"
            _write_rollout(path, [
                _meta(),
                _tokens("2026-09-03T09:01:00Z", 1, first, first),
                # A resumed/restarted segment begins its cumulative counter at zero.
                _tokens("2026-09-03T10:01:00Z", 2, second, second),
            ])
            result = why.reconcile_codex_rollout(path)

        self.assertTrue(result["matches"])
        self.assertEqual(result["segments"], 2)
        self.assertEqual(result["final"]["input_tokens"], 150)

    def test_attributes_each_call_from_latest_context_and_subagent_metadata(self):
        source = {"subagent": {"thread_spawn": {
            "parent_thread_id": "parent", "depth": 1,
            "agent_nickname": "Sagan", "agent_role": "worker",
        }}}
        rows = [
            _meta(source=source, originator="Codex Desktop"),
            _context("2026-09-03T09:00:00Z", "gpt-a", "low", "/work/first"),
            _tokens("2026-09-03T09:01:00Z", 1, {
                "input_tokens": 10, "output_tokens": 2,
            }, {"input_tokens": 10, "output_tokens": 2}),
            _context("2026-09-03T10:00:00Z", "gpt-b", "xhigh", "/work/second"),
            _tokens("2026-09-03T10:01:00Z", 2, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-context.jsonl", rows)
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual([row.model for row in records], ["gpt-a", "gpt-b"])
        self.assertEqual([row.effort for row in records], ["low", "xhigh"])
        self.assertEqual([row.project for row in records], ["first", "second"])
        self.assertTrue(all(row.thread_kind == "subagent" for row in records))
        self.assertTrue(all(row.agent == "Sagan" for row in records))
        self.assertTrue(all(row.entrypoint == "Codex Desktop" for row in records))

    def test_filters_on_event_time_in_resumed_old_rollout_and_ignores_noise(self):
        rows = [
            _meta(),
            {"timestamp": "2026-09-03T00:00:00Z", "type": "event_msg", "payload": "bad"},
            _context("2026-09-02T22:00:00Z", "gpt-test", "medium", "/work/project"),
            _tokens("2026-09-02T23:59:59Z", 1, {
                "input_tokens": 100, "output_tokens": 10,
            }, {"input_tokens": 100, "output_tokens": 10}),
            {"timestamp": "2026-09-03T00:00:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": None}},
            _tokens("2026-09-03T00:00:01Z", 2, {
                "input_tokens": 200, "output_tokens": 20,
            }, {"input_tokens": 300, "output_tokens": 30}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2025" / "01" / "01" / "rollout-old.jsonl"
            _write_rollout(path, rows)
            with path.open("a") as handle:
                handle.write("not json\n")
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].total_tokens, 220)
        self.assertIn(":2:", records[0].call_id)

    def test_result_order_is_deterministic_for_equal_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for session_id in ("z-session", "a-session"):
                _write_rollout(root / f"rollout-{session_id}.jsonl", [
                    _meta(session_id=session_id),
                    _context("2026-09-03T09:00:00Z", "gpt-test", "low", "/work/p"),
                    _tokens("2026-09-03T09:01:00Z", 1, {
                        "input_tokens": 10, "output_tokens": 1,
                    }),
                ])
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual([row.session_id for row in records], ["a-session", "z-session"])

    def test_call_ids_survive_windows_and_copied_rollouts(self):
        rows = [
            _meta(),
            _context("2026-09-02T23:00:00Z", "gpt-a", "high", "/work/project"),
            _tokens("2026-09-02T23:59:59Z", 41, {
                "input_tokens": 10, "output_tokens": 2,
            }, {"input_tokens": 10, "output_tokens": 2}),
            _tokens("2026-09-03T00:00:01Z", 42, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "rollout-original.jsonl"
            copied = root / "renamed-copy.jsonl"
            _write_rollout(first, rows)
            copied.write_text(first.read_text())
            narrow = why.collect_codex(root, self.start, self.end, paths=[first, copied])
            all_time = why.collect_codex(
                root,
                datetime(2026, 9, 2, tzinfo=timezone.utc),
                self.end,
                paths=[first, copied],
            )

        self.assertEqual(len(narrow), 1)
        self.assertEqual(narrow[0].call_id, "session-1:42:token_count")
        self.assertEqual(narrow[0].call_id, all_time[1].call_id)

    def test_missing_context_fields_reset_and_turn_metadata_is_derived(self):
        rows = [
            _meta(),
            {"timestamp": "2026-09-03T08:59:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "task-1"}},
            _context("2026-09-03T09:00:00Z", "gpt-a", "high", "/work/one"),
            _tokens("2026-09-03T09:01:00Z", 1, {
                "input_tokens": 10, "output_tokens": 2,
            }),
            {"timestamp": "2026-09-03T09:02:00Z", "type": "turn_context",
             "payload": {"cwd": "/work/two"}},
            _tokens("2026-09-03T09:03:00Z", 2, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rollout-context-reset.jsonl"
            _write_rollout(path, rows)
            records = why.collect_codex(root, self.start, self.end, paths=[path])

        self.assertEqual(records[0].turn_id, "task-1")
        self.assertEqual(records[0].turn_confidence, "observed")  # task_started names its turn explicitly
        self.assertEqual(records[0].model, "gpt-a")
        self.assertEqual(records[1].model, "unknown")
        self.assertEqual(records[1].effort, "unknown")

    def test_idless_session_meta_uses_synthetic_identity_per_event(self):
        def idless_rollout(timestamp, output):
            return [
                {"timestamp": "2026-09-03T08:00:00Z", "type": "session_meta",
                 "payload": {"model_provider": "openai"}},
                _tokens(timestamp, None, {"input_tokens": 1, "output_tokens": output},
                        {"input_tokens": output, "output_tokens": output}),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "rollout-a.jsonl"
            second = root / "rollout-b.jsonl"
            _write_rollout(first, idless_rollout("2026-09-03T08:01:00Z", 2))
            _write_rollout(second, idless_rollout("2026-09-03T08:02:00Z", 3))
            records = why.collect_codex(root, self.start, self.end, paths=[first, second])

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.id_synthetic for record in records))
        self.assertNotEqual(records[0].call_id, records[1].call_id)


def _tier(timestamp, tier="__missing__", **extra):
    settings = {"model": "gpt-test", "model_provider_id": "openai"}
    if tier != "__missing__":
        settings["service_tier"] = tier
    return {"timestamp": timestamp, "type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": settings, **extra}}


class CodexServiceTierTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def _collect(self, rows, source="cli"):
        calls = [r for r in rows if r.get("type") == "event_msg" and r["payload"].get("type") == "token_count"]
        self.assertTrue(calls)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-tier.jsonl", [_meta(source=source)] + rows)
            return [r.tariff for r in why.collect_codex(root, self.start, self.end)]

    @staticmethod
    def _call(minute, n):
        usage = {"input_tokens": 10 * n, "output_tokens": n}
        return _tokens(f"2026-09-03T09:{minute:02d}:00Z", n, usage, {"input_tokens": 10 * n, "output_tokens": n})

    def test_each_tier_maps_to_a_tariff(self):
        for tier, expected in (("default", {"service_tier": "standard"}), ("priority", {"service_tier": "fast"}),
                               ("fast", {"service_tier": "fast"}), ("flex", {"service_tier": "flex"})):
            with self.subTest(tier=tier):
                rows = [_tier("2026-09-03T09:00:00Z", tier, thread_id="t"), self._call(1, 1)]
                self.assertEqual(self._collect(rows), [expected])

    def test_no_event_or_missing_tier_leaves_tariff_none(self):
        self.assertEqual(self._collect([self._call(1, 1)]), [None])
        self.assertEqual(self._collect([_tier("2026-09-03T09:00:00Z"), self._call(1, 1)]), [None])

    def test_requests_before_the_first_event_stay_unrecorded(self):
        rows = [self._call(1, 1), _tier("2026-09-03T09:02:00Z", "priority"), self._call(3, 2)]
        self.assertEqual(self._collect(rows), [None, {"service_tier": "fast"}])

    def test_mid_thread_switch_applies_until_it_changes(self):
        rows = [_tier("2026-09-03T09:00:00Z", "default"), self._call(1, 1), self._call(2, 2),
                _tier("2026-09-03T09:03:00Z", "priority"), self._call(4, 3), self._call(5, 4),
                _tier("2026-09-03T09:06:00Z", "default"), self._call(7, 5)]
        std, fast = {"service_tier": "standard"}, {"service_tier": "fast"}
        self.assertEqual(self._collect(rows), [std, std, fast, fast, std])

    def test_a_snapshot_without_a_tier_resets_to_not_recorded(self):
        # Each thread_settings_applied row is a full snapshot: a later one without service_tier must not keep Fast.
        rows = [_tier("2026-09-03T09:00:00Z", "priority"), self._call(1, 1),
                _tier("2026-09-03T09:02:00Z"), self._call(3, 2)]
        self.assertEqual(self._collect(rows), [{"service_tier": "fast"}, None])

    def test_an_unknown_tier_is_kept_and_left_unpriced(self):
        rows = [_tier("2026-09-03T09:00:00Z", "priority"), self._call(1, 1),
                _tier("2026-09-03T09:02:00Z", "Turbo"), self._call(3, 2)]
        tariffs = self._collect(rows)
        self.assertEqual(tariffs, [{"service_tier": "fast"}, {"service_tier": "turbo"}])
        obs = {"provider": "openai", "model": "gpt-5.5", "tariff": tariffs[1], "complete": True,
               "tokens": {"fresh_input": 1000, "cache_read": 0, "cache_write": 0, "output": 10, "reasoning": 0}}
        from tokenatlas.pricing import load_prices, price_observation
        self.assertIsNone(price_observation(obs, load_prices())["cost"])

    def test_exec_and_subagent_rollouts_use_their_own_events(self):
        subagent = {"subagent": {"thread_spawn": {"parent_thread_id": "p", "agent_nickname": "Sagan"}}}
        for source in ("exec", subagent):
            with self.subTest(source=source):
                rows = [self._call(1, 1), _tier("2026-09-03T09:02:00Z", "priority"), self._call(3, 2)]
                self.assertEqual(self._collect(rows, source=source), [None, {"service_tier": "fast"}])

    def test_tier_does_not_leak_between_rollouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-a.jsonl", [_meta("a"), _tier("2026-09-03T09:00:00Z", "priority"), self._call(1, 1)])
            _write_rollout(root / "rollout-b.jsonl", [_meta("b"), self._call(2, 1)])
            got = {r.session_id: r.tariff for r in why.collect_codex(root, self.start, self.end)}
        self.assertEqual(got, {"a": {"service_tier": "fast"}, "b": None})

    def test_pricing_uses_fast_modifier_and_explicit_default_has_no_assumption(self):
        from tokenatlas.pricing import load_prices, price_observation
        table = load_prices()
        entry = next(m for m in table["models"] if m["provider"] == "openai" and "service_tier=fast" in (m.get("modifiers") or {}))
        base = dict(harness="codex", provider="openai", model=entry["model"], raw_usage={},
                    tokens={"fresh_input": 100_000, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0})
        fast = entry["modifiers"]["service_tier=fast"]
        std = price_observation(dict(base, tariff={"service_tier": "standard"}), table)
        self.assertEqual((std["status"], std["assumptions"]), ("priced", []))
        self.assertAlmostEqual(std["cost"], entry["input"] / 10)
        got = price_observation(dict(base, tariff={"service_tier": "fast"}), table)
        self.assertEqual((got["status"], got["assumptions"]), ("priced", []))
        self.assertAlmostEqual(got["cost"], fast["input"] / 10)
        self.assertEqual(price_observation(dict(base, tariff=None), table)["status"], "assumed")


def _quota_call(timestamp, n, rate_limits="__missing__"):
    row = _tokens(timestamp, n, {"input_tokens": 10 * n, "output_tokens": n}, {"input_tokens": 10 * n, "output_tokens": n})
    if rate_limits != "__missing__":
        row["payload"]["rate_limits"] = rate_limits
    return row


def _window(used=4.0, minutes=10080, resets=1791580407):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets}


def _limits(primary=None, secondary=None, **extra):
    return {"limit_id": "codex", "limit_name": None, "primary": primary, "secondary": secondary,
            "credits": {"has_credits": True, "unlimited": False, "balance": "60284.72"},
            "individual_limit": None, "spend_control_reached": None, "plan_type": "pro",
            "rate_limit_reached_type": None, **extra}


class CodexQuotaTests(unittest.TestCase):
    def _collect(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-quota.jsonl", [_meta()] + rows)
            return why.collect_codex(root, datetime(2026, 9, 3, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))

    def _quota(self, limits):
        records = self._collect([_quota_call("2026-09-03T09:01:00Z", 1, limits)])
        self.assertEqual(len(records), 1)
        return records[0].quota

    def test_weekly_only(self):
        quota = self._quota(_limits(primary=_window()))
        self.assertEqual(quota, {"limit_id": "codex", "plan_type": "pro", "reached": None, "windows": [
            {"slot": "primary", "minutes": 10080, "used_percent": 4.0, "resets_at": "2026-10-09T21:13:27+00:00"}]})

    def test_resets_at_is_utc_iso(self):
        quota = self._quota(_limits(primary=_window(resets=0.5 + 1700000000)))
        self.assertEqual(quota["windows"][0]["resets_at"], "2023-11-14T22:13:20.500000+00:00")

    def test_five_hour_and_weekly(self):
        quota = self._quota(_limits(primary=_window(12, 300, 1791000000), secondary=_window(40.5, 10080)))
        self.assertEqual([(w["slot"], w["minutes"], w["used_percent"]) for w in quota["windows"]],
                         [("primary", 300, 12.0), ("secondary", 10080, 40.5)])

    def test_secondary_null_and_overshoot_kept(self):
        quota = self._quota(_limits(primary=_window(104)))
        self.assertEqual([w["slot"] for w in quota["windows"]], ["primary"])
        self.assertEqual(quota["windows"][0]["used_percent"], 104.0)

    def test_missing_rate_limits_is_none(self):
        self.assertIsNone(self._quota("__missing__"))
        self.assertIsNone(self._quota(None))
        self.assertIsNone(self._quota("junk"))
        # a present snapshot with no window and no reached type is kept as an empty-window quota: it is the evidence of a recovery
        self.assertEqual(self._quota(_limits()), {"limit_id": "codex", "plan_type": "pro", "reached": None, "windows": []})
        self.assertIsNone(self._quota({"limit_name": "x", "primary": None}))

    def test_oversized_integers_never_abort_the_read(self):
        self.assertEqual(self._quota(_limits(primary=_window(10 ** 400)))["windows"], [])  # no float can hold it: the window is dropped
        quota = self._quota(_limits(primary=_window(4, 10080, 10 ** 400)))
        self.assertEqual((quota["windows"][0]["used_percent"], quota["windows"][0]["resets_at"]), (4.0, None))

    def test_malformed_windows_are_dropped(self):
        bad = [_window("4"), _window(-1), _window(float("nan")), _window(float("inf")), _window(True),
               {"used_percent": 4, "resets_at": 1791580407}, _window(4, 0), _window(4, -5), _window(4, True),
               _window(4, 60.5), _window(4, "300"), "x", 5]
        for window in bad:
            with self.subTest(window=window):
                self.assertEqual(self._quota(_limits(primary=window))["windows"], [])
        quota = self._quota(_limits(primary=_window("4"), secondary=_window(7, 10080)))
        self.assertEqual([w["slot"] for w in quota["windows"]], ["secondary"])

    def test_missing_or_bad_reset_keeps_window_without_reset(self):
        for resets in (None, "soon", -1, 0, True, float("nan"), 1e30):
            with self.subTest(resets=resets):
                quota = self._quota(_limits(primary=_window(resets=resets)))
                self.assertIsNone(quota["windows"][0]["resets_at"])

    def test_reached_type_is_kept_even_without_windows(self):
        quota = self._quota(_limits(rate_limit_reached_type="workspace_owner_credits_depleted"))
        self.assertEqual(quota, {"limit_id": "codex", "plan_type": "pro", "reached": "workspace_owner_credits_depleted", "windows": []})

    def test_credits_and_other_account_state_are_not_stored(self):
        quota = self._quota(_limits(primary=_window(), limit_name="Secret name", individual_limit={"x": 1},
                                    spend_control_reached=True))
        self.assertEqual(set(quota), {"limit_id", "plan_type", "reached", "windows"})
        self.assertNotIn("60284", json.dumps(quota))
        self.assertNotIn("Secret", json.dumps(quota))

    def test_hostile_strings_are_dropped(self):
        quota = self._quota(_limits(primary=_window(), plan_type={"a": 1}, limit_id="x" * 100))
        self.assertIsNone(quota["plan_type"])
        self.assertIsNone(quota["limit_id"])

    def test_repeated_cumulative_snapshot_is_one_record(self):
        first = _quota_call("2026-09-03T09:01:00Z", 1, _limits(primary=_window(4)))
        again = _quota_call("2026-09-03T09:01:05Z", 1, _limits(primary=_window(5)))
        records = self._collect([first, again])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].quota["windows"][0]["used_percent"], 4.0)  # the replayed snapshot is skipped before parsing

    def test_repeat_without_rate_limits_keeps_earlier_quota(self):
        first = _quota_call("2026-09-03T09:01:00Z", 1, _limits(primary=_window(4)))
        again = _quota_call("2026-09-03T09:01:05Z", 1)
        records = self._collect([first, again])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].quota["windows"][0]["used_percent"], 4.0)

    def test_rate_limit_only_event_creates_no_record(self):
        row = {"timestamp": "2026-09-03T09:00:00Z", "type": "event_msg",
               "payload": {"type": "token_count", "info": None, "rate_limits": _limits(primary=_window())}}
        self.assertEqual(self._collect([row]), [])

    def test_other_harness_records_have_no_quota(self):
        self.assertIsNone(why.AttributionRecord(
            harness="claude", provider="a", timestamp=datetime(2026, 9, 3, tzinfo=timezone.utc), session_id="s", call_id="c",
            model="m", effort="e", project="p", entrypoint="x", thread_kind="main", agent="main",
            fresh_input=1, cache_read=0, cache_write=0, output=0, reasoning=0).quota)


class CodexHostileMetadataTests(unittest.TestCase):
    def test_non_string_metadata_is_never_persisted(self):
        secret = {"prompt": "SECRET"}
        meta = _meta(session_id=secret, source={"subagent": {"thread_spawn": {
            "agent_nickname": secret, "parent_thread_id": secret}}}, originator=secret)
        meta["payload"].update(model_provider=secret, cwd=secret)
        counts = {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 2}
        rows = [meta, {"timestamp": "2026-09-03T10:00:00Z", "type": "turn_context",
                       "payload": {"model": secret, "effort": secret, "cwd": secret, "turn_id": secret}},
                _tokens("2026-09-03T10:00:01Z", 1, counts, counts)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-x.jsonl"
            _write_rollout(path, rows)
            records = why.collect_codex(Path(tmp), datetime(2026, 9, 3, tzinfo=timezone.utc),
                                        datetime(2026, 9, 4, tzinfo=timezone.utc))
        self.assertEqual(len(records), 1)
        self.assertNotIn("SECRET", repr(records[0]))
        record = records[0]
        self.assertEqual((record.provider, record.model, record.effort, record.entrypoint),
                         ("openai", "unknown", "unknown", "unknown"))


def _event(timestamp, kind, **payload):
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": kind, **payload}}


class CodexTurnAbortedTests(unittest.TestCase):
    def collect(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-abort.jsonl", [_meta()] + rows)
            return {r.call_id: r for r in why.collect_codex(
                root, datetime(2026, 9, 3, tzinfo=timezone.utc), datetime(2026, 9, 4, tzinfo=timezone.utc))}

    @staticmethod
    def call(minute, n, turn=None):
        usage = {"input_tokens": 10 * n, "output_tokens": n}
        row = _tokens(f"2026-09-03T09:{minute:02d}:00Z", n, usage, {"input_tokens": 10 * n, "output_tokens": n})
        if turn:
            row["payload"]["turn_id"] = turn
        return row

    def test_turn_aborted_flags_only_the_last_record_of_that_turn(self):
        got = self.collect([
            _event("2026-09-03T09:00:00Z", "task_started", turn_id="t1"), self.call(1, 1, "t1"), self.call(2, 2, "t1"),
            _event("2026-09-03T09:03:00Z", "turn_aborted", turn_id="t1"),
            _event("2026-09-03T09:04:00Z", "task_started", turn_id="t2"), self.call(5, 3, "t2"),
            _event("2026-09-03T09:06:00Z", "task_complete", turn_id="t2")])
        self.assertEqual({k.split(":")[1]: (r.turn_id, r.flags) for k, r in got.items()},
                         {"1": ("t1", None), "2": ("t1", ["interrupted"]), "3": ("t2", None)})

    def test_abort_without_a_request_in_its_turn_flags_nothing(self):
        got = self.collect([
            _event("2026-09-03T09:00:00Z", "task_started", turn_id="t1"), self.call(1, 1, "t1"),
            _event("2026-09-03T09:02:00Z", "task_complete", turn_id="t1"),
            _event("2026-09-03T09:03:00Z", "task_started", turn_id="t2"),
            _event("2026-09-03T09:04:00Z", "turn_aborted", turn_id="t2")])
        self.assertEqual([r.flags for r in got.values()], [None])

    def test_abort_without_turn_ids_flags_the_last_record_of_the_legacy_turn(self):
        got = self.collect([
            _event("2026-09-03T09:00:00Z", "task_started"), self.call(1, 1),
            _event("2026-09-03T09:02:00Z", "turn_aborted"),
            _event("2026-09-03T09:03:00Z", "task_started"), self.call(4, 2)])
        self.assertEqual({k.split(":")[1]: r.flags for k, r in got.items()}, {"1": ["interrupted"], "2": None})


if __name__ == "__main__":
    unittest.main()
