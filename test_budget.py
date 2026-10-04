"""Quota budget (issue #93): a manual plan size or calibration readings turn list price into a share of a limit."""
import base64
import contextlib
import gzip
import io
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tokenatlas import budget, pricing, prompts, quota_share, report, why
from tokenatlas.__main__ import main
from tokenatlas.history import History, _encode, normalize

UTC = timezone.utc
TABLE = pricing.load_prices()
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
MODEL = 'claude-sonnet-5-5'


def rec(id, when, out=1_000_000, harness='claude', turn=None, session='s1', plan='pro'):
    provider, model = ('anthropic', MODEL) if harness == 'claude' else ('openai', 'gpt-5.5')
    return dict(id=id, ts=when.isoformat(), harness=harness, provider=provider, machine='m', session=session, turn_id=turn or id, thread_kind='main', agent='main',
                model=model, complete=True, id_synthetic=False, effort=None, origin=None, turn_confidence='observed', parent_session=None, project_id=None,
                project_label=None, cwd=None, warnings=[], sources=[], raw_usage={}, tariff=None,
                quota={'limit_id': 'codex', 'plan_type': plan, 'reached': None, 'windows': []} if harness == 'codex' and plan else None,
                tokens=dict(fresh_input=0, cache_read=0, cache_write=0, output=out, reasoning=0))


def cost_of(records):
    return sum(prompts._cost(r, TABLE) for r in records)


def reading(used, cost, taken, minutes=10080, harness='claude'):
    return dict(harness=harness, minutes=minutes, used=used, resets_at=None, taken_at=taken.isoformat(), cost_usd=cost, unpriced_requests=0, approximate=False)


class Compute(unittest.TestCase):
    def test_one_reading_gives_cost_over_used(self):
        got = budget.derive(dict(readings=[reading(0.5, 10.0, NOW)], budgets=[]), NOW)[('claude', 10080, None)]
        self.assertEqual((got['budget_usd'], got['readings'], got['spread'], got['source']), (20.0, 1, [20.0, 20.0], 'readings'))
        self.assertEqual(got['date'], '2026-10-03')

    def test_several_readings_give_median_and_spread(self):
        data = dict(readings=[reading(0.5, 10.0, NOW - timedelta(days=i)) for i in range(1)] + [reading(0.25, 10.0, NOW), reading(0.1, 10.0, NOW - timedelta(days=7))], budgets=[])
        got = budget.derive(data, NOW)[('claude', 10080, None)]
        self.assertEqual((got['budget_usd'], got['spread'], got['readings']), (40.0, [20.0, 100.0], 3))
        self.assertEqual(got['date'], '2026-10-03')  # the latest reading

    def test_windows_and_harnesses_are_kept_apart(self):
        data = dict(readings=[reading(0.5, 10.0, NOW), reading(0.5, 3.0, NOW, minutes=300), reading(0.5, 8.0, NOW, harness='codex')], budgets=[])
        got = budget.derive(data, NOW)
        self.assertEqual({k: v['budget_usd'] for k, v in got.items()}, {('claude', 10080, None): 20.0, ('claude', 300, None): 6.0, ('codex', 10080, None): 16.0})

    def test_manual_budget_wins(self):
        data = dict(readings=[reading(0.5, 10.0, NOW)], budgets=[dict(harness='claude', minutes=10080, budget_usd=900.0, set_at=NOW.isoformat())])
        got = budget.derive(data, NOW)[('claude', 10080, None)]
        self.assertEqual((got['budget_usd'], got['source'], got['spread'], got['readings']), (900.0, 'manual', None, 0))

    def test_readings_expire_after_n_windows(self):
        old = reading(0.5, 10.0, NOW - timedelta(weeks=9))
        data = dict(readings=[old, reading(0.25, 10.0, NOW - timedelta(weeks=7))], budgets=[])
        self.assertEqual(budget.derive(data, NOW)[('claude', 10080, None)]['readings'], 1)
        self.assertEqual(budget.derive(data, NOW, keep=10)[('claude', 10080, None)]['readings'], 2)
        self.assertEqual(budget.derive(data, NOW, keep=1), {})
        five = dict(readings=[reading(0.5, 3.0, NOW - timedelta(hours=41), minutes=300)], budgets=[])
        self.assertEqual(budget.derive(five, NOW), {})  # 8 x 5 h = 40 h

    def test_malformed_readings_are_ignored(self):
        data = dict(readings=[dict(harness='claude'), reading(0, 10.0, NOW), reading(0.5, 0, NOW), reading(0.5, 10.0, NOW)], budgets=[dict(harness='x')])
        self.assertEqual(budget.derive(data, NOW)[('claude', 10080, None)]['readings'], 1)


class Reading(unittest.TestCase):
    def setUp(self):
        self.records = [rec('a', datetime(2026, 10, 1, 10, tzinfo=UTC)), rec('b', datetime(2026, 10, 2, 9, tzinfo=UTC), out=2_000_000),
                        rec('before', datetime(2026, 9, 1, tzinfo=UTC)), rec('x', datetime(2026, 10, 2, 9, tzinfo=UTC), harness='codex')]

    def make(self, **kw):
        args = dict(harness='claude', window='7d', used='52%', resets='2026-10-08T09:00:00+00:00', at='2026-10-03T12:00:00+00:00')
        args.update(kw)
        return budget.make_reading(self.records, TABLE, now=NOW, **args)

    def test_cost_is_the_harness_in_reset_minus_window_to_taken(self):
        r = self.make()
        self.assertEqual(r['cost_usd'], cost_of(self.records[:2]))
        self.assertEqual((r['used'], r['minutes'], r['approximate'], r['resets_at']), (0.52, 10080, False, '2026-10-08T09:00:00+00:00'))
        self.assertGreater(cost_of(self.records[:2]), 0)

    def test_without_resets_the_last_window_is_used_and_marked_approximate(self):
        r = self.make(resets=None, at='2026-10-03T12:00:00+00:00')
        self.assertTrue(r['approximate'])
        self.assertIsNone(r['resets_at'])
        self.assertEqual(r['cost_usd'], cost_of(self.records[:2]))
        self.assertEqual(self.make(resets=None, at='2026-10-02T08:00:00+00:00')['cost_usd'], cost_of(self.records[:1]))

    def test_codex_windows_are_trailing_whatever_resets_says(self):
        early = rec('early', datetime(2026, 10, 1, 9, tzinfo=UTC), harness='codex')  # before resets - 7d (2026-10-01T10:00) but inside [reading - 7d, reading]
        late = rec('late', datetime(2026, 10, 2, 9, tzinfo=UTC), harness='codex')
        records = [early, late]
        kw = dict(harness='codex', window='7d', used='50%', at='2026-10-03T12:00:00+00:00')
        with_resets = budget.make_reading(records, TABLE, resets='2026-10-08T10:00:00+00:00', now=NOW, **kw)
        without = budget.make_reading(records, TABLE, now=NOW, **kw)
        self.assertEqual(with_resets['cost_usd'], cost_of(records))
        self.assertEqual(without['cost_usd'], cost_of(records))
        self.assertFalse(without['approximate'])  # a trailing window is exact for Codex
        claude = [dict(early, harness='claude', provider='anthropic', model=MODEL), dict(late, harness='claude', provider='anthropic', model=MODEL)]
        fixed = budget.make_reading(claude, TABLE, now=NOW, **dict(kw, harness='claude'), resets='2026-10-08T10:00:00+00:00')
        self.assertEqual(fixed['cost_usd'], cost_of(claude[1:]))  # Claude keeps [resets - window, reading]
        with self.assertRaisesRegex(ValueError, 'not after the reading'):
            budget.make_reading(records, TABLE, resets='2026-10-03T11:00:00+00:00', now=NOW, **kw)

    def test_only_the_subscription_provider_counts(self):
        mine = rec('mine', datetime(2026, 10, 2, 9, tzinfo=UTC), harness='codex')
        other = dict(rec('other', datetime(2026, 10, 2, 9, tzinfo=UTC), out=9_000_000, harness='codex'), provider='openrouter')
        start, end = datetime(2026, 10, 1, tzinfo=UTC), NOW
        self.assertEqual(budget.cost_seen([mine, other], TABLE, 'codex', start, end, 'pro')[0], cost_of([mine]))
        alias = dict(TABLE, provider_aliases={'chatgpt': 'openai'})
        self.assertEqual(budget.cost_seen([dict(mine, provider='chatgpt')], alias, 'codex', start, end, 'pro')[0], cost_of([mine]))
        records = [mine, other]
        assigned = [('codex', r['session'], r['turn_id'], 'own') for r in records]
        got = budget.turn_costs(records, assigned, lambda r: prompts._cost(r, TABLE), TABLE)
        self.assertEqual(list(got), [('codex', 's1', 'mine')])  # the other-provider turn has no entry, so no calibrated share

    def test_a_tiny_cost_is_stored_at_full_precision(self):
        tiny = [rec('t', datetime(2026, 10, 2, 9, tzinfo=UTC), out=3)]  # well under $0.00005
        r = budget.make_reading(tiny, TABLE, 'claude', '7d', '50%', '2026-10-08T09:00:00+00:00', '2026-10-03T12:00:00+00:00', NOW)
        self.assertEqual(r['cost_usd'], cost_of(tiny))
        self.assertTrue(0 < r['cost_usd'] < 0.00005)
        self.assertTrue(budget._valid(r))
        self.assertGreater(budget.derive(dict(readings=[r], budgets=[]), NOW)[('claude', 10080, None)]['budget_usd'], 0)

    def test_ambiguous_requests_are_left_out_of_the_cost_seen(self):
        mixed = self.records[:1] + [dict(rec('syn', datetime(2026, 10, 2, 9, tzinfo=UTC), out=5_000_000), id_synthetic=True)]
        self.assertEqual(budget.cost_seen(mixed, TABLE, 'claude', datetime(2026, 10, 1, tzinfo=UTC), NOW)[0], cost_of(self.records[:1]))

    def test_percent_forms(self):
        self.assertEqual(budget.parse_used('52%'), 0.52)
        self.assertEqual(budget.parse_used('52'), 0.52)
        self.assertEqual(budget.parse_used('100%'), 1.0)
        self.assertEqual(budget.parse_used('0,5%'), 0.005)

    def test_invalid_inputs_are_rejected_with_a_reason(self):
        for kw, text in ((dict(used='0%'), 'above 0%'), (dict(used='101%'), 'at most 100%'), (dict(used='-3'), 'not a percentage'), (dict(used='half'), 'not a percentage'),
                         (dict(resets='next friday'), 'not a time'), (dict(at='yesterday'), 'not a time'), (dict(resets='2026-10-03T11:00:00+00:00'), 'not after the reading'),
                         (dict(resets='2026-10-20T11:00:00+00:00'), 'more than a 7d'), (dict(window='30d'), 'must be one of'), (dict(harness='pi'), 'must be one of')):
            with self.assertRaisesRegex(ValueError, text):
                self.make(**kw)

    def test_a_window_with_no_cost_seen_is_rejected_and_explained(self):
        with self.assertRaisesRegex(ValueError, 'saw no priced codex pro-plan usage.*quota set'):
            budget.make_reading([rec('a', datetime(2026, 1, 1, tzinfo=UTC), harness='codex')], TABLE, 'codex', '7d', '40%', '2026-10-08T09:00:00+00:00', '2026-10-03T12:00:00+00:00', NOW)

    def test_naive_times_are_machine_local(self):
        self.assertEqual(budget.parse_time('2026-10-09 21:00', 'x'), datetime(2026, 10, 9, 21).astimezone().astimezone(UTC))
        self.assertEqual(budget.parse_time('2026-10-09T21:00Z', 'x'), datetime(2026, 10, 9, 21, tzinfo=UTC))


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state' / budget.FILE

    def test_file_is_private_and_written_atomically(self):
        budget.set_budget(self.path, 'claude', '7d', 900, NOW)
        if os.name != 'nt':  # Windows has no POSIX permission bits
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [budget.FILE])
        before = self.path.read_text()
        with patch('tokenatlas.budget.os.replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                budget.set_budget(self.path, 'claude', '7d', 1, NOW)
        self.assertEqual(self.path.read_text(), before)  # the old file is intact
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [budget.FILE])  # no temporary file left

    def test_an_identical_retry_replaces_the_reading(self):
        budget.add_reading(self.path, reading(0.5, 10.0, NOW))
        budget.add_reading(self.path, reading(0.25, 10.0, NOW - timedelta(days=1)))
        before = budget.derive(budget.load(self.path), NOW)[('claude', 10080, None)]
        budget.add_reading(self.path, reading(0.5, 10.0, NOW))  # the same harness, window and time
        data = budget.load(self.path)
        self.assertEqual(len(data['readings']), 2)
        after = budget.derive(data, NOW)[('claude', 10080, None)]
        self.assertEqual((after['budget_usd'], after['readings'], after['spread']), (before['budget_usd'], 2, before['spread']))
        budget.add_reading(self.path, reading(0.4, 10.0, NOW))  # a corrected value for the same reading wins
        self.assertEqual(sorted(x['used'] for x in budget.load(self.path)['readings']), [0.25, 0.4])

    def test_a_damaged_file_warns_on_stderr_but_a_missing_one_is_quiet(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(budget.load_derived(self.path), {})  # no file: quiet
        self.assertEqual(err.getvalue(), '')
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{not json')
        with contextlib.redirect_stderr(err):
            self.assertEqual(budget.load_derived(self.path), {})
        self.assertRegex(err.getvalue(), r'^usage: warning: cannot read .*quota-budget\.json')

    def test_set_replaces_and_forget_filters(self):
        budget.set_budget(self.path, 'claude', '7d', 900, NOW)
        budget.set_budget(self.path, 'claude', '7d', 800, NOW)
        budget.set_budget(self.path, 'codex', '5h', 20, NOW)
        budget.add_reading(self.path, reading(0.5, 10.0, NOW))
        self.assertEqual(len(budget.load(self.path)['budgets']), 2)
        self.assertEqual(budget.forget(self.path, 'claude', '7d'), 2)
        self.assertEqual((len(budget.load(self.path)['budgets']), len(budget.load(self.path)['readings'])), (1, 0))
        self.assertEqual(budget.forget(self.path), 1)
        self.assertEqual(budget.forget(self.path), 0)

    def test_invalid_budgets_are_rejected_and_a_damaged_file_is_an_error(self):
        for bad in (0, -5, float('nan'), float('inf')):
            with self.assertRaisesRegex(ValueError, 'positive'):
                budget.set_budget(self.path, 'claude', '7d', bad)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{not json')
        with self.assertRaisesRegex(ValueError, 'cannot read'):
            budget.load(self.path)
        self.assertEqual(budget.load_derived(self.path), {})


class Marking(unittest.TestCase):
    derived = {('claude', 10080, None): dict(budget_usd=100.0, source='readings', readings=3, spread=[80.0, 130.0], date='2026-10-03')}

    def item(self, share=None, harness='claude', turn='t'):
        return dict(harness=harness, session='s', turn_id=turn, quota_share=share)

    def test_calibrated_only_without_an_observed_or_estimated_share(self):
        observed = dict(window_minutes=10080, delta_percent=3, label='observed', before=1, after=4, shared_with=0)
        estimate = dict(observed, label='estimate', delta_percent=2.5)
        unknown = dict(observed, label='unknown', delta_percent=None)
        costs = {('claude', 's', 't'): (4.0, False), ('pi', 's', 't'): (4.0, False)}
        items = budget.mark_turns([self.item(), self.item(observed), self.item(estimate), self.item(unknown), self.item(turn='none'), self.item(harness='pi')], self.derived, costs)
        self.assertEqual([i['quota_share'] and i['quota_share']['label'] for i in items], ['calibrated', 'observed', 'estimate', 'calibrated', None, None])
        got = items[0]['quota_share']
        self.assertEqual((got['delta_percent'], got['window_minutes'], got['lower_bound']), (4.0, 10080, False))
        self.assertEqual(got['calibration'], dict(budget_usd=100.0, readings=3, spread=[80.0, 130.0], date='2026-10-03', source='readings'))
        self.assertEqual(items[2]['quota_share']['delta_percent'], 2.5)

    def test_without_a_budget_nothing_changes(self):
        self.assertEqual(budget.mark_turns([self.item()], {}, {('claude', 's', 't'): (4.0, False)}), [self.item()])

    def test_the_weekly_budget_is_preferred_and_the_five_hour_one_is_the_fallback(self):
        both = {**self.derived, ('claude', 300, None): dict(budget_usd=20.0, source='manual', readings=0, spread=None, date='2026-10-01')}
        self.assertEqual(budget.share(both, 'claude', 4.0)['window_minutes'], 10080)
        five = {k: v for k, v in both.items() if k[1] == 300}
        got = budget.share(five, 'claude', 4.0)
        self.assertEqual((got['window_minutes'], got['delta_percent'], got['calibration']['source']), (300, 20.0, 'manual'))

    def test_text_in_top(self):
        item = budget.share(self.derived, 'claude', 4.0)
        self.assertEqual(quota_share.line(item, 'claude'), '≈4% of your weekly Claude limit (your calibration, 2026-10-03)')
        self.assertEqual(quota_share.line(budget.share(self.derived, 'claude', 0.2), 'claude'), '< 1% of your weekly Claude limit (your calibration, 2026-10-03)')
        self.assertEqual(quota_share.line(dict(item, label='observed', delta_percent=3), 'claude'), '~3% of weekly Claude limit')

    def test_a_lower_bound_is_a_floor_and_never_under_1_percent(self):
        floor = quota_share.line(budget.share(self.derived, 'claude', 3.9, True), 'claude')
        self.assertEqual(floor, '≥3% of your weekly Claude limit (your calibration, 2026-10-03)')
        unknown = quota_share.line(budget.share(self.derived, 'claude', 0.2, True), 'claude')
        self.assertTrue(unknown.startswith('share of your weekly Claude limit: unknown'), unknown)
        self.assertNotIn('< 1%', unknown)
        self.assertTrue(budget.share(self.derived, 'claude', 0.2, True)['lower_bound'])

    def test_boundaries_use_full_precision(self):
        derived = {('claude', 10080, None): dict(budget_usd=1000.0, source='manual', readings=0, spread=None, date='2026-10-03')}
        near1, near4 = budget.share(derived, 'claude', 9.96, True), budget.share(derived, 'claude', 39.99, True)  # 0.996% and 3.999%
        self.assertEqual((near1['delta_percent'], near4['delta_percent']), (0.99, 3.99))  # a floor is never rounded up
        self.assertTrue(quota_share.line(near1, 'claude').startswith('share of your weekly Claude limit: unknown'))
        self.assertTrue(quota_share.line(near4, 'claude').startswith('≥3% of your weekly'))
        exact = budget.share(derived, 'claude', 4.999, False)  # 0.4999%: rounds to 0, which is '< 1%'
        self.assertEqual(exact['delta_percent'], 0.5)
        self.assertTrue(quota_share.line(exact, 'claude').startswith('< 1% of'))
        self.assertTrue(quota_share.line(budget.share(derived, 'claude', 9.96, False), 'claude').startswith('≈1% of'))

    def test_turn_costs_identified_only_and_flags_what_is_missing(self):
        def a(r):
            return ('claude', r['session'], r['turn_id'], 'own')
        t = datetime(2026, 10, 2, tzinfo=UTC)
        ok = rec('ok', t, turn='one')
        syn = dict(rec('syn', t, out=9_000_000, turn='syn'), id_synthetic=True)
        mixed = [rec('m1', t, turn='mix'), dict(rec('m2', t, out=9_000_000, turn='mix'), id_synthetic=True)]
        unpriced = rec('u', t, turn='unp')
        unpriced['model'] = 'no-such-model'
        incomplete = dict(rec('i', t, turn='inc'), complete=False)
        records = [ok, syn, *mixed, unpriced, incomplete]
        got = budget.turn_costs(records, [a(r) for r in records], lambda r: prompts._cost(r, TABLE))
        one = cost_of([ok])
        self.assertEqual(got[('claude', 's1', 'one')], (one, False, None))
        self.assertEqual(got[('claude', 's1', 'syn')], (0.0, True, None))
        self.assertEqual(got[('claude', 's1', 'mix')], (one, True, None))
        self.assertEqual(got[('claude', 's1', 'unp')], (0.0, True, None))
        self.assertEqual(got[('claude', 's1', 'inc')], (one, True, None))


class Plans(unittest.TestCase):
    T = datetime(2026, 10, 2, 9, tzinfo=UTC)
    kw = dict(harness='codex', window='7d', used='50%', at='2026-10-03T12:00:00+00:00')

    def make(self, records, **extra):
        return budget.make_reading(records, TABLE, now=NOW, **{**self.kw, **extra})

    def test_cost_seen_counts_only_the_readings_plan_and_counts_unknown_plan_requests(self):
        pro, team = rec('pro', self.T, harness='codex', plan='pro'), rec('team', self.T, out=9_000_000, harness='codex', plan='team')
        none = rec('none', self.T, out=5_000_000, harness='codex', plan=None)  # no quota snapshot: plan unknown
        start = self.T - timedelta(days=1)
        self.assertEqual(budget.cost_seen([pro, team, none], TABLE, 'codex', start, NOW, 'pro'), (cost_of([pro]), 0, 1))
        self.assertEqual(budget.cost_seen([pro, team, none], TABLE, 'codex', start, NOW, 'team'), (cost_of([team]), 0, 1))
        self.assertEqual(self.make([pro, team, none], plan='Team')['cost_usd'], cost_of([team]))  # --plan, case-insensitive
        self.assertEqual(self.make([pro, team, none], plan='team')['excluded_requests'], 1)

    def test_the_plan_defaults_to_the_one_in_the_window_else_the_latest_and_several_is_an_error(self):
        pro, team = rec('pro', self.T, harness='codex', plan='pro'), rec('team', self.T + timedelta(hours=1), harness='codex', plan='team')
        self.assertEqual(self.make([pro])['plan'], 'pro')
        with self.assertRaisesRegex(ValueError, 'several Codex plans.*pro, team.*--plan'):
            self.make([pro, team])
        self.assertEqual(self.make([pro, team], plan='pro')['cost_usd'], cost_of([pro]))
        old = rec('old', datetime(2026, 8, 1, tzinfo=UTC), harness='codex', plan='plus')  # outside the window: the latest known plan is the default, but no cost
        with self.assertRaisesRegex(ValueError, 'saw no priced codex plus-plan usage'):
            self.make([old])
        with self.assertRaisesRegex(ValueError, 'no Codex plan is known'):
            self.make([])
        with self.assertRaisesRegex(ValueError, '--plan is only for Codex'):
            budget.make_reading([rec('a', self.T)], TABLE, 'claude', '7d', '50%', plan='pro', at='2026-10-03T12:00:00+00:00', now=NOW)
        self.assertIsNone(budget.make_reading([rec('a', self.T)], TABLE, 'claude', '7d', '50%', at='2026-10-03T12:00:00+00:00', now=NOW)['plan'])

    def test_budgets_and_readings_are_kept_per_plan(self):
        data = dict(readings=[dict(reading(0.5, 10.0, NOW, harness='codex'), plan='pro'), dict(reading(0.5, 40.0, NOW, harness='codex'), plan='team')],
                    budgets=[dict(harness='codex', minutes=10080, plan=None, budget_usd=500.0, set_at=NOW.isoformat())])
        got = budget.derive(data, NOW)
        self.assertEqual({k: v['budget_usd'] for k, v in got.items()}, {('codex', 10080, 'pro'): 20.0, ('codex', 10080, 'team'): 80.0, ('codex', 10080, None): 500.0})
        self.assertEqual(budget.share(got, 'codex', 10.0, False, 'pro')['calibration']['budget_usd'], 20.0)
        self.assertEqual(budget.share(got, 'codex', 10.0, False, 'plus')['calibration']['budget_usd'], 500.0)  # a plan without its own falls back to the plan-less manual budget
        self.assertIsNone(budget.share({k: v for k, v in got.items() if k[2]}, 'codex', 10.0, False, 'plus'))
        self.assertIsNone(budget.share(got, 'codex', 10.0, False, budget.MIXED))
        self.assertEqual(sorted(b['plan'] or '' for b in budget.public(got)), ['', 'pro', 'team'])

    def test_a_manual_plan_budget_is_stored_per_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / budget.FILE
            budget.set_budget(path, 'codex', '7d', 100, NOW, 'Pro')
            budget.set_budget(path, 'codex', '7d', 200, NOW, 'team')
            budget.set_budget(path, 'codex', '7d', 300, NOW, 'pro')  # replaces the first
            self.assertEqual(sorted((b['plan'], b['budget_usd']) for b in budget.load(path)['budgets']), [('pro', 300.0), ('team', 200.0)])
            with self.assertRaisesRegex(ValueError, '--plan is only for Codex'):
                budget.set_budget(path, 'claude', '7d', 100, NOW, 'pro')

    def test_turn_costs_scope_turns_by_plan(self):
        pro, team = rec('p', self.T, harness='codex', plan='pro', turn='tp'), rec('t', self.T, harness='codex', plan='team', turn='tt')
        mixed = [rec('m1', self.T, harness='codex', plan='pro', turn='tm'), rec('m2', self.T, harness='codex', plan='team', turn='tm')]
        unknown = rec('u', self.T, harness='codex', plan=None, turn='tu')
        partly = [rec('q1', self.T, harness='codex', plan='pro', turn='tq'), rec('q2', self.T, harness='codex', plan=None, turn='tq')]
        records = [pro, team, *mixed, unknown, *partly]
        got = budget.turn_costs(records, [('codex', r['session'], r['turn_id'], 'own') for r in records], lambda r: prompts._cost(r, TABLE), TABLE)
        one = cost_of([pro])
        self.assertEqual(got[('codex', 's1', 'tp')], (one, False, 'pro'))
        self.assertEqual(got[('codex', 's1', 'tt')], (one, False, 'team'))
        self.assertEqual(got[('codex', 's1', 'tm')][1:], (True, budget.MIXED))
        self.assertEqual(got[('codex', 's1', 'tu')], (0.0, True, None))  # excluded and counted: a floor of nothing
        self.assertEqual(got[('codex', 's1', 'tq')], (one, True, 'pro'))


class Accounts(unittest.TestCase):
    """Two accounts on one plan share limit id, plan and reset time; their counters interleave (quota_share._split_counters)."""
    RESETS = '2026-10-09T09:00:00+00:00'

    def cx(self, id, minute, session, percent, plan='pro', turn=None):
        r = rec(id, datetime(2026, 10, 2, 9, minute, tzinfo=UTC), harness='codex', plan=plan, session=session, turn=turn)
        r['quota'] = {'limit_id': 'codex', 'plan_type': plan, 'reached': None, 'windows': [{'slot': 'secondary', 'minutes': 10080, 'used_percent': float(percent), 'resets_at': self.RESETS}]}
        return r

    def two_accounts(self):
        levels = [('a', 60), ('b', 99), ('a', 61), ('b', 98), ('a', 62), ('b', 99), ('a', 63), ('b', 98)]
        return [self.cx(f'r{i}', i, session, pct, turn=f't_{session}{i}') for i, (session, pct) in enumerate(levels)]

    def one_account(self):
        return [self.cx(f'r{i}', i, 'a' if i % 2 else 'b', 60 + i // 2) for i in range(8)]

    kw = dict(harness='codex', window='7d', used='50%', at='2026-10-03T12:00:00+00:00')

    def test_two_accounts_on_one_plan_are_detected_and_calibration_is_rejected(self):
        records = self.two_accounts()
        self.assertEqual(budget.multi_counter_plans(records), {'pro'})
        with self.assertRaisesRegex(ValueError, 'more than one codex account on the pro plan.*single account.*quota set.*--plan pro'):
            budget.make_reading(records, TABLE, now=NOW, **self.kw)
        self.assertEqual(budget.multi_counter_plans(records, datetime(2026, 10, 2, 10, tzinfo=UTC), NOW), frozenset())  # outside the window nothing conflicts

    def test_one_account_and_other_plans_are_not_affected(self):
        self.assertEqual(budget.multi_counter_plans(self.one_account()), frozenset())
        self.assertEqual(budget.make_reading(self.one_account(), TABLE, now=NOW, **self.kw)['plan'], 'pro')
        other = self.two_accounts() + [self.cx('t', 30, 'c', 5, plan='team')]
        self.assertEqual(budget.multi_counter_plans(other), {'pro'})
        self.assertEqual(budget.make_reading(other, TABLE, now=NOW, plan='team', **self.kw)['plan'], 'team')

    def test_turns_on_a_multi_account_plan_get_no_calibrated_share(self):
        records = self.two_accounts() + [self.cx('t', 30, 'c', 5, plan='team', turn='team-turn')]
        assigned = [('codex', r['session'], r['turn_id'], 'own') for r in records]
        got = budget.turn_costs(records, assigned, lambda r: prompts._cost(r, TABLE), TABLE)
        derived = {('codex', 10080, 'pro'): dict(budget_usd=100.0, source='manual', readings=0, spread=None, date='2026-10-03'),
                   ('codex', 10080, 'team'): dict(budget_usd=100.0, source='manual', readings=0, spread=None, date='2026-10-03')}
        items = [dict(harness='codex', session=k[1], turn_id=k[2], quota_share=None) for k in got]
        budget.mark_turns(items, derived, got)
        shares = {i['turn_id']: i['quota_share'] for i in items}
        self.assertEqual(shares['team-turn']['label'], 'calibrated')
        self.assertEqual([v for k, v in shares.items() if k != 'team-turn' and v is not None], [])


class Malformed(unittest.TestCase):
    def test_malformed_entries_are_skipped_and_counted(self):
        good = reading(0.5, 10.0, NOW)
        data = dict(readings=[good, 'x', None, dict(good, taken_at=None), dict(good, used=float('nan')), dict(good, cost_usd='9'), dict(good, used=True)],
                    budgets=[dict(harness='claude', minutes=300, budget_usd=5.0, set_at=None), dict(harness='claude', minutes=10080, budget_usd=True, set_at=NOW.isoformat()),
                             'y', dict(harness='codex', minutes=10080, budget_usd=7.0, set_at=NOW.isoformat())])
        got = budget.derive(data, NOW)
        self.assertEqual(sorted(got), [('claude', 10080, None), ('codex', 10080, None)])
        self.assertEqual(got[('claude', 10080, None)]['source'], 'readings')
        self.assertEqual(budget.invalid_entries(data), 9)

    def test_timezone_naive_stored_times_are_invalid_not_a_crash(self):
        good = reading(0.5, 10.0, NOW)
        naive = '2026-10-03T12:00:00'
        data = dict(readings=[good, dict(good, taken_at=naive), dict(good, resets_at=naive), dict(good, resets_at=None), dict(good, resets_at='2026-10-08T09:00:00+00:00')],
                    budgets=[dict(harness='claude', minutes=300, budget_usd=5.0, set_at=naive), dict(harness='codex', minutes=300, budget_usd=5.0, set_at=NOW.isoformat())])
        got = budget.derive(data, NOW)
        self.assertEqual(got[('claude', 10080, None)]['readings'], 3)
        self.assertNotIn(('claude', 300, None), got)
        self.assertEqual(budget.invalid_entries(data), 3)

    def test_set_and_calibrate_survive_a_malformed_file_and_drop_non_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / budget.FILE
            path.write_text(json.dumps(dict(readings=['x', None, dict(reading(0.5, 10.0, NOW))], budgets=['y', None, dict(harness='codex', minutes=300, budget_usd=5.0, set_at=NOW.isoformat())])))
            budget.set_budget(path, 'claude', '7d', 900, NOW)
            data = budget.load(path)
            self.assertEqual((len(data['readings']), len(data['budgets'])), (1, 2))
            self.assertEqual(budget.invalid_entries(data), 0)
            path.write_text(json.dumps(dict(readings=['x'], budgets=[None])))
            budget.add_reading(path, reading(0.5, 10.0, NOW))
            self.assertEqual((len(budget.load(path)['readings']), len(budget.load(path)['budgets'])), (1, 0))
            path.write_text(json.dumps(dict(readings=['x', reading(0.5, 10.0, NOW)], budgets=[None])))
            self.assertEqual(budget.forget(path, 'claude', '7d'), 3)  # the reading, plus the two malformed entries

    def test_show_warns_and_does_not_crash_and_forget_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / budget.FILE
            path.write_text(json.dumps(dict(readings=['x'], budgets=[dict(harness='claude', minutes=10080, budget_usd=9.0, set_at=None)])))
            args = lambda **kw: type('A', (), {**dict(quota='show', json=False, keep=8), **kw})()
            lines = []
            budget.run(args(), Path(tmp) / 'h.sqlite3', None, None, lines.append)
            self.assertIn('warning: 2 malformed entries', lines[0])
            lines.clear()
            budget.run(args(json=True), Path(tmp) / 'h.sqlite3', None, None, lines.append)
            self.assertEqual(json.loads(lines[0])['invalid_entries'], 2)
            self.assertEqual(budget.forget(path, 'claude'), 2)  # the malformed budget and the non-dict reading


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'h.sqlite3'
        with History(self.db) as h:
            for i, (day, out) in enumerate(((1, 1_000_000), (2, 2_000_000))):
                r = why.AttributionRecord(harness='claude', provider='anthropic', timestamp=datetime(2026, 10, day, 9, tzinfo=UTC), session_id='s1', call_id=f'c{i}', model=MODEL,
                                          effort=None, project='app', entrypoint='cli', thread_kind='main', agent='main', fresh_input=0, cache_read=0, cache_write=0, output=out,
                                          reasoning=0, turn_id=f't{i}', turn_confidence='derived', raw_usage={'input_tokens': 0, 'output_tokens': out, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})
                h._insert(h.connection, _encode(normalize(r, 'm')))
            h.connection.commit()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.dict(os.environ, {'XDG_STATE_HOME': str(Path(self.tmp.name) / 'xdg')}):
            try:
                code = main(['--db', str(self.db), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def calibrate(self, used='50%', at='2026-10-03T12:00:00+00:00'):
        return self.run_cli('quota', 'calibrate', '--harness', 'claude', '--window', '7d', '--used', used, '--resets', '2026-10-08T09:00:00+00:00', '--at', at)

    def test_calibrate_show_forget_round_trip(self):
        with History(self.db) as h:
            spend = cost_of(h.records())
        code, out, _ = self.calibrate()
        self.assertEqual(code, 0, out)
        self.assertIn('not an exact limit', out)
        path = budget.path_for(self.db)
        if os.name != 'nt':  # Windows has no POSIX permission bits
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.parent, self.db.parent)
        code, out, _ = self.run_cli('quota', 'show', '--json', '--keep', '1000')
        shown = json.loads(out)
        self.assertEqual(shown['derived'][0]['budget_usd'], round(spend, 4) / 0.5)
        self.assertEqual((shown['derived'][0]['readings'], len(shown['readings'])), (1, 1))
        self.assertIn('median of 1 reading', self.run_cli('quota', 'show', '--keep', '1000')[1])
        self.assertEqual(self.run_cli('quota', 'forget', '--harness', 'claude')[0], 0)
        self.assertEqual(json.loads(self.run_cli('quota', 'show', '--json')[1])['derived'], [])

    def test_bad_input_exits_2_with_the_reason(self):
        for used, text in (('0%', 'above 0%'), ('130%', 'at most 100%')):
            code, _, err = self.calibrate(used=used)
            self.assertEqual(code, 2)
            self.assertIn(text, err)
        code, _, err = self.calibrate(at='tomorrowish')
        self.assertEqual((code, 'not a time' in err), (2, True))
        code, _, err = self.run_cli('quota', 'calibrate', '--harness', 'codex', '--window', '5h', '--used', '10%')
        self.assertEqual((code, 'no Codex plan is known' in err), (2, True))
        self.assertFalse(budget.path_for(self.db).exists())

    def test_top_json_shows_a_calibrated_share_only_with_a_budget(self):
        top = lambda: json.loads(self.run_cli('top', '--json', '--limit', '5')[1])['prompts']
        self.assertEqual([p['quota_share'] for p in top()], [None, None])
        self.assertNotIn('≈', self.run_cli('top')[1])
        self.assertEqual(self.run_cli('quota', 'set', '--harness', 'claude', '--window', '7d', '--budget-usd', '100')[0], 0)
        shares = {p['cost']: p['quota_share'] for p in top()}
        self.assertEqual({c: s['label'] for c, s in shares.items()}, {c: 'calibrated' for c in shares})
        big = shares[max(shares)]
        self.assertEqual(big['delta_percent'], round(max(shares) / 100 * 100, 2))
        self.assertEqual(big['calibration']['budget_usd'], 100.0)
        self.assertEqual(big['calibration']['spread'], None)
        text = self.run_cli('top')[1]
        self.assertRegex(text, r'≈\d+% of your weekly Claude limit \(your calibration, \d{4}-\d\d-\d\d\)')

    def add(self, call_id, when, out, turn, synthetic=False):
        with History(self.db) as h:
            r = why.AttributionRecord(harness='claude', provider='anthropic', timestamp=when, session_id='s1', call_id=call_id, model=MODEL, effort=None, project='app',
                                      entrypoint='cli', thread_kind='main', agent='main', fresh_input=0, cache_read=0, cache_write=0, output=out, reasoning=0, turn_id=turn,
                                      turn_confidence='derived', id_synthetic=synthetic,
                                      raw_usage={'input_tokens': 0, 'output_tokens': out, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})
            h._insert(h.connection, _encode(normalize(r, 'm')))
            h.connection.commit()

    def top(self, *args):
        return {p['turn_id']: p for p in json.loads(self.run_cli('top', '--json', '--limit', '10', *args)[1])['prompts']}

    def test_a_filter_through_a_turn_still_calibrates_from_the_whole_turn(self):
        self.add('late', datetime(2026, 10, 2, 9, 30, tzinfo=UTC), 1_000_000, 't1')  # t1 now has an hour-apart second request
        self.run_cli('quota', 'set', '--harness', 'claude', '--window', '7d', '--budget-usd', '100')
        whole = self.top()['t1']
        cut = self.top('--start', '2026-10-02T09:15:00+00:00')['t1']  # the window keeps only the second request of the turn
        self.assertLess(cut['cost'], whole['cost'])
        self.assertEqual(cut['quota_share']['delta_percent'], whole['quota_share']['delta_percent'])
        self.assertEqual(whole['quota_share']['label'], 'calibrated')

    def test_ambiguous_requests_never_count_and_make_a_floor(self):
        self.add('amb', datetime(2026, 10, 1, 9, 5, tzinfo=UTC), 50_000_000, 't0', synthetic=True)
        self.run_cli('quota', 'set', '--harness', 'claude', '--window', '7d', '--budget-usd', '100')
        got = self.top()
        share = got['t0']['quota_share']
        self.assertTrue(share['lower_bound'])
        self.assertEqual(share['delta_percent'], round(10.0 / 100 * 100, 2))  # the identified 1M output tokens only
        self.assertIn('≥10% of your weekly Claude limit', self.run_cli('top')[1])
        self.assertFalse(got['t1']['quota_share']['lower_bound'])

    def test_help_describes_claude_and_codex_windows_separately(self):
        code, out, _ = self.run_cli('quota', 'calibrate', '--help')
        text = ' '.join(out.split())
        self.assertEqual(code, 0)
        for needle in ('Claude: anchors the window [resets - window, reading]', 'trailing 5h/7d and the reading is marked approximate', 'Codex: the window is always the trailing 5h/7d', 'only recorded'):
            self.assertIn(needle, text)
        readme = (Path(__file__).parent / 'README.md').read_text(encoding='utf-8')
        self.assertIn('**Codex:** always the trailing window', readme)

    def test_top_prices_reprices_a_reading_so_the_share_does_not_move_with_the_table(self):
        self.assertEqual(self.calibrate(used='50%')[0], 0)  # priced with the packaged table; the stored reading carries its identity
        self.assertEqual(budget.load(budget.path_for(self.db))['readings'][0]['table'], budget.table_id(TABLE))
        table = json.loads(json.dumps(TABLE))
        entry = next(m for m in table['models'] if m['model'] == MODEL)
        for k in ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output'):
            if entry.get(k):
                entry[k] *= 2  # every price doubled
        prices = Path(self.tmp.name) / 'prices.json'
        prices.write_text(json.dumps(table))
        base = {p['turn_id']: p for p in json.loads(self.run_cli('top', '--json', '--limit', '5')[1])['prompts']}
        doubled = {p['turn_id']: p for p in json.loads(self.run_cli('top', '--json', '--limit', '5', '--prices', str(prices))[1])['prompts']}
        self.assertTrue(all(p['quota_share'] and p['quota_share']['label'] == 'calibrated' for p in base.values()))
        for turn, p in doubled.items():
            self.assertAlmostEqual(p['cost'], 2 * base[turn]['cost'])
            # the calibration is repriced with the same table, so cost / budget is unchanged: the share does not move
            self.assertAlmostEqual(p['quota_share']['exact_percent'], base[turn]['quota_share']['exact_percent'], places=9)
        # without a history to reprice from the reading is stale and gives no calibrated share
        self.assertEqual(budget.load_derived(budget.path_for(self.db), table=table), {})
        path = budget.path_for(self.db)
        data = budget.load(path)
        data['readings'][0]['table'] = 'old:000000000000'
        budget.save(path, data)
        self.assertIn('1 reading priced with another price table', self.run_cli('quota', 'show', '--keep', '1000')[1])

    def test_relative_price_changes_move_shares_after_repricing(self):
        # two models; the reading was priced with table A, the selected table B doubles one model: costs and the budget are recomputed, so the shares change
        cheap = rec('c', datetime(2026, 10, 2, 9, tzinfo=UTC), turn='cheap')
        dear = dict(rec('d', datetime(2026, 10, 2, 10, tzinfo=UTC), turn='dear'), model='claude-opus-5-5')
        records = [cheap, dear]
        read = budget.make_reading(records, TABLE, 'claude', '7d', '50%', '2026-10-08T09:00:00+00:00', '2026-10-03T12:00:00+00:00', NOW)
        table = json.loads(json.dumps(TABLE))
        for m in table['models']:
            if m['model'] == 'claude-opus-5-5':
                for k in ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output'):
                    if m.get(k):
                        m[k] *= 2
        assigned = [('claude', 's1', r['turn_id'], 'own') for r in records]
        shares = lambda table, data: {k[2]: budget.share(budget.derive(data, NOW), 'claude', *v)['exact_percent'] for k, v in
                                       budget.turn_costs(records, assigned, lambda r: prompts._cost(r, table), table).items()}
        before = shares(TABLE, dict(readings=[read], budgets=[]))
        after = shares(table, budget.reprice(dict(readings=[read], budgets=[]), table, lambda: records))
        self.assertNotAlmostEqual(before['cheap'], after['cheap'], places=6)  # the unchanged model's share moves too: the budget grew with the other model's price
        self.assertNotAlmostEqual(before['dear'], after['dear'], places=6)
        self.assertAlmostEqual(after['cheap'] + after['dear'], before['cheap'] + before['dear'], places=6)  # the turns still add up to the reading's 50%

    def test_report_state_changes_with_a_budget(self):
        html = Path(self.tmp.name) / 'r.html'
        self.assertEqual(self.run_cli('report', '--html', str(html), '--if-changed')[0], 0)
        self.assertIn('"skipped": true', self.run_cli('report', '--html', str(html), '--if-changed')[1])
        self.run_cli('quota', 'set', '--harness', 'claude', '--window', '7d', '--budget-usd', '100')
        self.assertNotIn('"skipped": true', self.run_cli('report', '--html', str(html), '--if-changed')[1])


class Report(unittest.TestCase):
    def build(self, **kw):
        recs = [rec('a', datetime(2026, 10, 1, 9, tzinfo=UTC)), rec('b', datetime(2026, 10, 2, 9, tzinfo=UTC), out=2_000_000)]
        derived = [dict(harness='claude', minutes=10080, source='readings', budget_usd=900.0, readings=3, spread=[700.0, 1100.0], date='2026-10-03')]
        return report.build_report(recs, {}, now=NOW, budgets=kw.pop('budgets', derived), **kw)

    def test_payload_has_calibrated_shares_and_the_note(self):
        payload = self.build(redact=False)
        shares = payload['quota_shares']
        self.assertEqual({v['label'] for v in shares.values()}, {'calibrated'})
        self.assertEqual(sorted(round(v['percent'], 2) for v in shares.values()), [round(c / 9, 2) for c in sorted((cost_of([rec('a', NOW)]), cost_of([rec('b', NOW, 2_000_000)])))])
        self.assertEqual(next(iter(shares.values()))['date'], '2026-10-03')
        self.assertEqual(payload['quota_calibration'], [dict(harness='claude', minutes=10080, plan=None, source='readings', budget_usd=900.0, readings=3, spread=[700.0, 1100.0], date='2026-10-03')])

    def test_shared_report_has_no_calibrated_share_or_budget(self):
        payload = self.build(redact=True)
        self.assertNotIn('quota_calibration', payload)
        self.assertNotIn('quota_shares', payload)  # with the turn costs a percentage would give the budget away
        blob = json.dumps(payload)
        for needle in ('calibrated', 'calibration', '"lower_bound": true'):
            self.assertNotIn(needle, blob)
        self.assertNotIn('"budget_usd"', blob)

    def test_the_card_percent_is_full_precision_for_the_page_to_floor(self):
        t = datetime(2026, 10, 1, 9, tzinfo=UTC)
        turn = [rec('p', t, turn='t'), dict(rec('q', t, turn='t'), id_synthetic=True)]  # $10 identified + an ambiguous request: a floor
        derived = [dict(harness='claude', minutes=10080, source='manual', budget_usd=1004.0, readings=0, spread=None, date='2026-10-03')]
        card = next(iter(report.build_report(turn, {}, redact=False, now=NOW, budgets=derived)['quota_shares'].values()))
        self.assertAlmostEqual(card['percent'], 10 / 1004 * 100, places=12)  # 0.996...: the page floors it to 0 and shows 'unknown', not '≥ 1 %'
        self.assertTrue(card['lower_bound'])

    def test_the_card_uses_the_whole_turn_and_flags_a_floor(self):
        t = datetime(2026, 10, 1, 9, tzinfo=UTC)
        whole = [rec('p', t, turn='t'), dict(rec('q', t + timedelta(minutes=1), out=9_000_000, turn='t'), id_synthetic=True), rec('u', t + timedelta(minutes=2), turn='t')]
        whole[2]['model'] = 'no-such-model'
        derived = [dict(harness='claude', minutes=10080, source='manual', budget_usd=100.0, readings=0, spread=None, date='2026-10-03')]
        # a report filtered to the first request only still prices the card from the whole turn (universe)
        payload = report.build_report(whole[:1], {}, redact=False, universe=whole, now=NOW, budgets=derived)
        card = next(iter(payload['quota_shares'].values()))
        self.assertEqual((card['label'], card['lower_bound'], card['percent']), ('calibrated', True, round(cost_of(whole[:1]) / 100 * 100, 12)))

    def test_nothing_changes_without_a_calibration(self):
        plain = self.build(budgets=None)
        self.assertNotIn('quota_shares', plain)
        self.assertNotIn('quota_calibration', plain)
        self.assertEqual(self.build(budgets=[]), plain)

    def test_strings_and_markup_in_both_languages(self):
        page = report.render_report(self.build(redact=False))
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        self.assertEqual(i18n['sv']['qs_cal'], '≈ {n} % av {w} (din kalibrering)')
        self.assertEqual(i18n['en']['qs_cal'], '≈ {n}% of {w} (your calibration)')
        for lang in ('sv', 'en'):
            for key in ('qs_lt1_cal', 'qs_cal_title', 'qc_title', 'qc_p', 'qc_manual', 'qc_readings'):
                self.assertTrue(i18n[lang][key], (lang, key))
        self.assertIn('id="quota-calibration" class="hidden"', page)
        for lang in ('sv', 'en'):
            self.assertTrue(i18n[lang]['qs_cal_lb'] and i18n[lang]['qs_cal_unknown'], lang)
            self.assertNotIn('inflat', i18n[lang]['qc_p'])
        self.assertIn('för liten', i18n['sv']['qc_p'])
        self.assertIn('too small', i18n['en']['qc_p'])
        self.assertIn("s.lower_bound", page.split('<script', 1)[0] + page)


RESETS = datetime(2026, 10, 8, 9, tzinfo=UTC)


def hit(at, cost_window=(None, None), reached='five_hour', minutes=300, harness='claude', plan=None, resets=None, limit_id=None):
    """A limit hit as limits.limit_hits shapes it (only the fields budget reads)."""
    resets = resets or at + timedelta(hours=1)
    start = resets - timedelta(minutes=minutes) if harness == 'claude' else at - timedelta(minutes=minutes)
    return dict(harness=harness, at=at.isoformat(), reached=reached, window_minutes=minutes, resets_at=resets.isoformat(), rolling=harness != 'claude',
                window=dict(start=start.isoformat(), end=at.isoformat()), origin=dict(plan_type=plan, limit_id=limit_id))


def snap(t, used, minutes=10080, due=RESETS):
    return dict(harness='claude', account='claude', t=t, used_percent=used, window=(minutes, due.isoformat()), key=('claude', 'claude', minutes, due.isoformat(), None, 0, 0))


class Automatic(unittest.TestCase):
    """Issue #116: budgets from limit hits and statusline readings already in the history; never stored."""
    T = datetime(2026, 10, 3, 9, tzinfo=UTC)

    def test_a_budget_from_one_limit_hit(self):
        recs = [rec('a', self.T - timedelta(hours=2)), rec('b', self.T - timedelta(hours=1), out=2_000_000)]
        auto, skipped = budget.auto_budgets(recs, [hit(self.T)], [], TABLE)
        got = auto[('claude', 300, None)]
        self.assertAlmostEqual(got['budget_usd'], cost_of(recs))  # 100% of the window is what the logs saw in it
        self.assertEqual((got['source'], got['points'], got['spread'], skipped), ('limit_hit', 1, [got['budget_usd']] * 2, {}))
        self.assertEqual((got['first_date'], got['date']), ('2026-10-03', '2026-10-03'))

    def test_a_budget_from_several_limit_hits_is_the_median_of_the_last_windows(self):
        recs, hits = [], []
        for i, out in enumerate((1_000_000, 3_000_000, 2_000_000)):
            t = self.T + timedelta(days=i)
            recs.append(rec(f'r{i}', t - timedelta(hours=1), out=out))
            hits.append(hit(t))
        auto, _ = budget.auto_budgets(recs, hits, [], TABLE)
        costs = sorted(cost_of([r]) for r in recs)
        got = auto[('claude', 300, None)]
        self.assertAlmostEqual(got['budget_usd'], costs[1])
        self.assertEqual((got['points'], got['windows']), (3, 3))
        self.assertEqual([round(v, 6) for v in got['spread']], [round(costs[0], 6), round(costs[2], 6)])
        self.assertEqual(budget.auto_budgets(recs, hits, [], TABLE, keep=1)[0][('claude', 300, None)]['points'], 1)  # only the latest window

    def test_a_budget_from_statusline_rises(self):
        recs = [rec('a', self.T + timedelta(minutes=30), out=2_000_000)]
        snaps = [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 30)]
        auto, _ = budget.auto_budgets(recs, [], snaps, TABLE)
        got = auto[('claude', 10080, None)]
        self.assertAlmostEqual(got['budget_usd'], cost_of(recs) / 0.2)
        self.assertEqual((got['source'], got['points']), ('statusline', 1))

    def test_small_movements_are_ignored_and_accumulate_to_the_threshold(self):
        recs = [rec('a', self.T + timedelta(minutes=30), out=2_000_000), rec('b', self.T + timedelta(minutes=90), out=2_000_000)]
        small = [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 14), snap(self.T + timedelta(minutes=100), 9)]
        self.assertEqual(budget.auto_budgets(recs, [], small, TABLE), ({}, {}))
        # 10 -> 14 is ignored, but the anchor stays at 10, so 10 -> 16 is one point of 6 points over the cost since the anchor
        more = [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 14), snap(self.T + timedelta(minutes=100), 16)]
        got = budget.auto_budgets(recs, [], more, TABLE)[0][('claude', 10080, None)]
        self.assertAlmostEqual(got['budget_usd'], cost_of(recs) / 0.06)
        self.assertEqual(got['points'], 1)

    def test_a_window_with_little_cost_is_skipped_and_counted(self):
        tiny = [rec('a', self.T + timedelta(minutes=30), out=1_000)]
        auto, skipped = budget.auto_budgets(tiny, [hit(self.T + timedelta(hours=1))], [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 40)], TABLE)
        self.assertEqual((auto, skipped), ({}, {'little_cost': 2}))

    def test_unpriced_and_ambiguous_requests(self):
        t = self.T + timedelta(minutes=30)
        unpriced = [rec('a', t, out=2_000_000), dict(rec('b', t), model='no-such-model')]
        snaps = [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 40)]
        self.assertEqual(budget.auto_budgets(unpriced, [hit(self.T + timedelta(hours=1))], snaps, TABLE), ({}, {'unpriced': 2}))
        ambiguous = [rec('a', t, out=2_000_000), dict(rec('b', t, out=50_000_000), id_synthetic=True)]  # an ambiguous request is in no cost
        got = budget.auto_budgets(ambiguous, [], snaps, TABLE)[0][('claude', 10080, None)]
        self.assertAlmostEqual(got['budget_usd'], cost_of(ambiguous[:1]) / 0.3)

    def test_only_the_subscription_provider_counts(self):
        t = self.T + timedelta(minutes=30)
        other = [dict(rec('a', t, out=2_000_000), provider='openrouter')]
        self.assertEqual(budget.auto_budgets(other, [hit(self.T + timedelta(hours=1))], [snap(self.T, 10), snap(self.T + timedelta(minutes=60), 40)], TABLE), ({}, {'little_cost': 2}))

    def test_codex_window_full_hits_are_a_per_plan_fallback(self):
        recs = [rec('a', self.T - timedelta(hours=1), harness='codex', plan='pro', out=2_000_000), rec('b', self.T - timedelta(hours=1), harness='codex', plan='team', out=9_000_000)]
        auto, skipped = budget.auto_budgets(recs, [hit(self.T, reached='window_full', harness='codex', plan='pro')], [], TABLE)
        self.assertEqual(list(auto), [('codex', 300, 'pro')])
        self.assertAlmostEqual(auto[('codex', 300, 'pro')]['budget_usd'], cost_of(recs[:1]))
        self.assertEqual(budget.auto_budgets(recs, [hit(self.T, reached='window_full', harness='codex')], [], TABLE), ({}, {'no_plan': 1}))

    def test_two_accounts_counters_in_one_window_are_not_one_80_point_rise(self):
        # two sessions report different counters (10% and 90%) of one window instance; the real snapshot processing splits them into two counters
        base = datetime(2026, 10, 3, 9, tzinfo=UTC)
        recs = [rec(f'x{i}', base + timedelta(minutes=i), out=2_000_000, session='sa' if i % 2 else 'sb') for i in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'claude-quota.jsonl'
            rows = [dict(ts=(base + timedelta(minutes=i, seconds=30)).isoformat(), session='sa' if i % 2 else 'sb', seven_day=dict(used_percent=10 if i % 2 else 90, resets_at=RESETS.isoformat()))
                    for i in range(8)]
            path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
            snaps = quota_share.snapshots_from_records(recs, claude=path)
        self.assertEqual(len({s['key'][5] for s in snaps}), 2)
        self.assertEqual(budget.auto_budgets(recs, [], snaps, TABLE), ({}, {'ambiguous': 1}))
        # a limit hit in the same split window is not one inflated budget either (and a hit in another window still counts)
        window_hit = hit(base + timedelta(minutes=8), minutes=10080, reached='seven_day', resets=RESETS)
        self.assertEqual(budget.auto_budgets(recs, [window_hit], snaps, TABLE), ({}, {'ambiguous': 2}))
        later = hit(base + timedelta(days=10), minutes=10080, reached='seven_day', resets=RESETS + timedelta(days=7))
        got, skipped = budget.auto_budgets(recs + [rec('late', base + timedelta(days=9), out=2_000_000)], [later], snaps, TABLE)
        self.assertEqual((list(got), skipped), ([('claude', 10080, None)], {'ambiguous': 1}))

    def test_a_custom_codex_limit_never_feeds_or_receives_the_default_budget(self):
        custom = lambda r: dict(r, quota=dict(r['quota'], limit_id='codex_bengalfox'))
        t = self.T - timedelta(hours=1)
        recs = [rec('d', t, harness='codex', out=2_000_000), custom(rec('c', t, harness='codex', out=9_000_000))]
        hits = [hit(self.T, reached='window_full', harness='codex', plan='pro', limit_id='codex'), hit(self.T, reached='window_full', harness='codex', plan='pro', limit_id='codex_bengalfox')]
        auto, skipped = budget.auto_budgets(recs, hits, [], TABLE)
        self.assertEqual(skipped, {'other_limit': 1})
        self.assertAlmostEqual(auto[('codex', 300, 'pro')]['budget_usd'], cost_of(recs[:1]))  # the custom limit's request is not in the default window
        assigned = prompts.assign_prompts(recs)
        costs = budget.turn_costs(recs, assigned, lambda r: prompts._cost(r, TABLE), TABLE)
        self.assertEqual(costs[('codex', 's1', 'd')][2], 'pro')
        self.assertEqual(costs[('codex', 's1', 'c')][2], budget.MIXED)  # no default-limit budget fits a turn on another limit
        self.assertIsNone(budget.share(auto, 'codex', *costs[('codex', 's1', 'c')]))

    def test_a_plan_less_custom_limit_request_makes_the_turn_mixed(self):
        t = self.T
        custom = dict(rec('c', t + timedelta(minutes=1), harness='codex', turn='t', out=9_000_000), quota={'limit_id': 'codex_bengalfox', 'plan_type': None, 'reached': None, 'windows': []})
        recs = [rec('d', t, harness='codex', plan='pro', turn='t', out=2_000_000), custom]
        costs = budget.turn_costs(recs, prompts.assign_prompts(recs), lambda r: prompts._cost(r, TABLE), TABLE)
        self.assertEqual(costs[('codex', 's1', 't')][2], budget.MIXED)

    def test_manual_budgets_and_readings_win(self):
        auto = {('claude', 10080, None): dict(budget_usd=50.0, source='limit_hit', readings=1, points=1, windows=1, spread=[50.0, 50.0], first_date='2026-10-03', date='2026-10-03'),
                ('claude', 300, None): dict(budget_usd=9.0, source='statusline', readings=1, points=1, windows=1, spread=[9.0, 9.0], first_date='2026-10-03', date='2026-10-03')}
        manual = {('claude', 10080, None): dict(budget_usd=100.0, source='manual', readings=0, spread=None, date='2026-10-01')}
        merged = budget.combine(manual, auto)
        self.assertEqual(merged, manual)  # not even the automatic 5-hour one is mixed in
        self.assertEqual(budget.share(merged, 'claude', 4.0)['label'], 'calibrated')
        self.assertEqual(budget.combine({}, auto), auto)
        self.assertEqual(budget.share(auto, 'claude', 5.0)['label'], 'auto-calibrated')

    def test_an_auto_share_only_without_an_observed_or_estimated_share(self):
        auto = {('claude', 10080, None): dict(budget_usd=100.0, source='limit_hit', readings=2, points=2, windows=2, spread=[80.0, 130.0], first_date='2026-09-20', date='2026-10-03')}
        observed = dict(window_minutes=10080, delta_percent=3, label='observed', before=1, after=4, shared_with=0)
        items = [dict(harness='claude', session='s', turn_id=t, quota_share=q) for t, q in (('a', None), ('b', observed), ('c', dict(observed, label='estimate')))]
        costs = {('claude', 's', t): (4.0, False, None) for t in 'abc'}
        budget.mark_turns(items, auto, costs)
        self.assertEqual([i['quota_share']['label'] for i in items], ['auto-calibrated', 'observed', 'estimate'])
        self.assertEqual(quota_share.line(items[0]['quota_share'], 'claude'), '≈4% of the weekly Claude limit (estimated from your limit hits)')
        sl = dict(auto[('claude', 10080, None)], source='statusline')
        self.assertEqual(quota_share.line(budget.share({('claude', 300, None): sl}, 'claude', 20.0), 'claude'), '≈20% of the 5-hour Claude limit (estimated from your statusline readings)')
        lb = budget.share(auto, 'claude', 4.0, lower_bound=True)
        self.assertEqual(quota_share.line(lb, 'claude'), '≥4% of the weekly Claude limit (estimated from your limit hits)')

    def report(self, redact):
        t = self.T
        recs = [rec('a', t - timedelta(hours=2)), rec('b', t - timedelta(hours=1), out=2_000_000), rec('c', t + timedelta(days=1))]
        return report.build_report(recs, {}, now=NOW, redact=redact, all_hits=[hit(t)])

    def test_private_report_has_auto_shares_and_shared_report_has_none(self):
        private = self.report(False)
        self.assertEqual({v['label'] for v in private['quota_shares'].values()}, {'auto-calibrated'})
        self.assertEqual({v['source'] for v in private['quota_shares'].values()}, {'limit_hit'})
        self.assertEqual(private['quota_calibration'][0]['source'], 'limit_hit')
        shared = json.dumps(self.report(True))
        for needle in ('auto-calibrated', 'quota_calibration', 'quota_shares', 'budget_usd', 'limit_hit"'):
            self.assertNotIn(needle, shared)

    def test_strings_in_both_languages(self):
        page = report.render_report(self.report(False))
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        for lang in ('sv', 'en'):
            for key in ('qs_auto', 'qs_auto_lt1', 'qs_auto_lb', 'qs_auto_unknown', 'qs_auto_title', 'qc_auto', 'qs_src_limit_hit', 'qs_src_statusline', 'qs_src_limit_hit_statusline'):
                self.assertTrue(i18n[lang][key], (lang, key))
        self.assertEqual(i18n['en']['qs_auto'], '≈ {n}% of {w} (estimated from {src})')


class AutomaticCli(unittest.TestCase):
    run_cli = Cli.run_cli

    def setUp(self):
        Cli.setUp(self)
        rows = [dict(ts='2026-10-02T08:00:00+00:00', session='s1', seven_day=dict(used_percent=10, resets_at=RESETS.isoformat())),
                dict(ts='2026-10-02T09:30:00+00:00', session='s1', seven_day=dict(used_percent=40, resets_at=RESETS.isoformat()))]
        (self.db.parent / 'claude-quota.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    def test_quota_show_lists_automatic_budgets_separately(self):
        with History(self.db) as h:
            spend = cost_of([r for r in h.records() if r['ts'].startswith('2026-10-02')])
        code, out, _ = self.run_cli('quota', 'show', '--json')
        shown = json.loads(out)
        self.assertEqual((code, shown['derived'], shown['readings']), (0, [], []))
        (got,) = shown['automatic']
        self.assertEqual((got['harness'], got['minutes'], got['source'], got['points']), ('claude', 10080, 'statusline', 1))
        self.assertAlmostEqual(got['budget_usd'], spend / 0.3)
        self.assertEqual((got['first_date'], got['date'], shown['automatic_skipped']), ('2026-10-02', '2026-10-02', {}))
        code, text, _ = self.run_cli('quota', 'show')
        self.assertIn('automatic budgets', text)
        self.assertIn('statusline, median of 1 point in 1 window', text)
        self.assertFalse(budget.path_for(self.db).exists())  # computed, never stored

    def quota_file(self, *rows):
        lines = [dict(ts=ts, session='s1', seven_day=dict(used_percent=used, resets_at=due)) for ts, used, due in rows]
        (self.db.parent / 'claude-quota.jsonl').write_text('\n'.join(json.dumps(r) for r in lines) + '\n')

    def test_keep_reaches_the_automatic_derivation(self):
        a, b = '2026-10-05T09:00:00+00:00', '2026-10-12T09:00:00+00:00'
        self.quota_file(('2026-10-01T08:00:00+00:00', 10, a), ('2026-10-01T09:30:00+00:00', 40, a), ('2026-10-02T08:00:00+00:00', 10, b), ('2026-10-02T09:30:00+00:00', 40, b))
        full = json.loads(self.run_cli('quota', 'show', '--json')[1])['automatic'][0]
        self.assertEqual((full['points'], full['windows']), (2, 2))
        one = json.loads(self.run_cli('quota', 'show', '--json', '--keep', '1')[1])['automatic'][0]
        self.assertEqual((one['points'], one['windows']), (1, 1))

    def test_skipped_points_are_shown_without_any_budget(self):
        self.quota_file(('2026-10-05T08:00:00+00:00', 10, RESETS.isoformat()), ('2026-10-05T09:30:00+00:00', 40, RESETS.isoformat()))  # no requests in between
        text = self.run_cli('quota', 'show')[1]
        self.assertIn('no budget yet', text)
        self.assertIn('automatic points skipped: 1 little cost', text)
        self.assertNotIn('automatic budgets (', text)

    def test_top_gives_old_claude_turns_an_auto_share_and_a_manual_budget_wins(self):
        code, out, _ = self.run_cli('top', '-n', '5', '--json')
        self.assertEqual(code, 0, out)
        labels = {p['quota_share']['label'] for p in json.loads(out)['prompts'] if p['quota_share']}
        self.assertEqual(labels, {'auto-calibrated'})
        self.run_cli('quota', 'set', '--harness', 'claude', '--window', '7d', '--budget-usd', '1000')
        labels = {p['quota_share']['label'] for p in json.loads(self.run_cli('top', '-n', '5', '--json')[1])['prompts'] if p['quota_share']}
        self.assertEqual(labels, {'calibrated'})


if __name__ == '__main__':
    unittest.main()
