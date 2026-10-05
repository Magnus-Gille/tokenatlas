"""Share of the weekly / 5-hour limit per turn and per window (issue #90)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import limits, pricing, prompts, quota_share as qs

T0 = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
TABLE = pricing.load_prices()
RESET = (T0 + timedelta(days=3)).isoformat()
RESET5 = (T0 + timedelta(hours=4)).isoformat()


def iso(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def quota(week=None, five=None, reached=None, resets=RESET, resets5=RESET5, plan='pro', limit_id='codex'):
    windows = []
    if five is not None:
        windows.append({'slot': 'primary', 'minutes': 300, 'used_percent': float(five), 'resets_at': resets5})
    if week is not None:
        windows.append({'slot': 'secondary', 'minutes': 10080, 'used_percent': float(week), 'resets_at': resets})
    return {'limit_id': limit_id, 'plan_type': plan, 'reached': reached, 'windows': windows}


def req(id, ts, turn='t1', session='s1', out=1000, **kw):
    return dict(id=id, ts=ts, harness='codex', provider='openai', machine='m', session=session, turn_id=turn, thread_kind=kw.pop('thread_kind', 'main'), agent='main',
                model=kw.pop('model', 'gpt-5.5'), complete=kw.pop('complete', True), id_synthetic=kw.pop('id_synthetic', False), effort=None, origin=None, turn_confidence='observed', parent_session=kw.pop('parent_session', None),
                project_id=None, project_label=None, cwd=None, warnings=[], sources=[], raw_usage={}, tariff=None,
                tokens=dict(fresh_input=0, cache_read=0, cache_write=0, output=out, reasoning=0), quota=kw.pop('q', None), **kw)


def shares(records):
    snaps = qs.snapshots_from_records(records)
    return snaps, qs.largest(qs.turn_shares(records, snaps, TABLE))


K1, K2 = ('codex', 's1', 't1'), ('codex', 's2', 't2')


class Snapshots(unittest.TestCase):
    def test_one_per_window_skipping_rejected_and_unreset(self):
        rejected = req('x', iso(1), q={**quota(week=5), 'status': 'rejected'})
        noreset = req('y', iso(2), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': None,
                                      'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': 1.0, 'resets_at': None}]})
        both = req('z', iso(3), q=quota(week=5, five=2))
        snaps = qs.snapshots_from_records([rejected, noreset, both, req('plain', iso(4))])
        self.assertEqual(sorted(s['window'][0] for s in snaps), [300, 10080])
        self.assertEqual({(s['harness'], s['account'], s['plan_type'], s['turn']) for s in snaps}, {('codex', 'codex', 'pro', K1)})

    def test_resets_at_drift_is_one_window_instance_and_a_real_drop_is_a_new_one(self):
        a = (datetime.fromisoformat(RESET) + timedelta(seconds=3)).isoformat()
        b = (datetime.fromisoformat(RESET) + timedelta(days=7)).isoformat()
        recs = [req('a', iso(1), q=quota(week=10)), req('b', iso(2), q=quota(week=11, resets=a)), req('c', iso(3), q=quota(week=2, resets=b))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual([s['window'][1] for s in snaps], [a, a, b])  # the latest reset time reported is the instance's context
        self.assertEqual(len(qs.windows(snaps)), 2)

    def test_account_falls_back_to_harness(self):
        snaps = qs.snapshots_from_records([req('a', iso(1), q=quota(week=5, limit_id=None))])
        self.assertEqual(snaps[0]['account'], 'codex')

    def test_windows_peak_hit_and_credits(self):
        recs = [req('a', iso(1), q=quota(week=10)), req('b', iso(2), q=quota(week=100, reached='rate_limit_reached')),
                req('c', iso(3), q=quota(week=95)), req('d', iso(4), q=quota(week=5, resets='2026-09-20T00:00:00+00:00', reached='workspace_owner_credits_depleted'))]
        ws = qs.windows(qs.snapshots_from_records(recs))
        self.assertEqual(len(ws), 2)
        first = ws[0]
        self.assertEqual((first['minutes'], first['peak_percent'], first['peak_at'], first['first_percent'], first['snapshots'], first['hit']),
                         (10080, 100.0, iso(2), 10.0, 3, True))
        self.assertEqual(first['start'], (datetime.fromisoformat(RESET) - timedelta(minutes=10080)).isoformat())
        self.assertFalse(ws[1]['hit'])  # depleted credits are not a hit of the window

    def test_reached_marks_only_the_window_that_is_full(self):
        recs = [req('a', iso(1), q=quota(week=40, five=100, reached='rate_limit_reached'))]
        by = {w['minutes']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs))}
        self.assertEqual(by, {300: True, 10080: False})
        both = [req('a', iso(1), q=quota(week=100, five=100, reached='rate_limit_reached'))]
        self.assertEqual({w['hit'] for w in qs.windows(qs.snapshots_from_records(both))}, {None})  # ambiguous: which window was reached is unknown
        none = [req('a', iso(1), q=quota(week=40, five=60, reached='rate_limit_reached'))]
        self.assertEqual({w['hit'] for w in qs.windows(qs.snapshots_from_records(none))}, {False})

    def test_window_cost_excludes_ambiguous_identities(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000, id_synthetic=True)]
        cost = lambda r: prompts._cost(r, TABLE)
        both = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        one = qs.windows(qs.snapshots_from_records(recs[:1]), records=recs[:1], cost_of=cost)[0]
        self.assertAlmostEqual(both['cost'], one['cost'])
        self.assertGreater(both['cost'], 0)

    def test_last_keeps_the_most_recent_per_length(self):
        recs = [req(str(i), iso(i * 20000), q=quota(week=i, resets=(T0 + timedelta(days=3 + 7 * i)).isoformat())) for i in range(5)]  # more than a window apart
        ws = qs.windows(qs.snapshots_from_records(recs), last=2)
        self.assertEqual([w['first_percent'] for w in ws], [3.0, 4.0])

    def test_window_cost(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=lambda r: prompts._cost(r, TABLE))
        self.assertGreater(ws[0]['cost'], 0)
        self.assertEqual(ws[0]['unpriced_requests'], 0)
        self.assertNotIn('key', ws[0])


class Shares(unittest.TestCase):
    def test_single_turn_observed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=11)), req('b', iso(11), q=quota(week=13))]
        _, got = shares(recs)
        s = got[K1]
        self.assertEqual((s['label'], s['estimate']), ('observed', None))
        self.assertEqual(s['observed'], dict(before=10.0, after=13.0, delta=3.0, shared_with=0))
        self.assertEqual(s['window_key'][2:4], (10080, RESET))
        self.assertEqual(qs.text(s), '~3% of weekly Codex limit')
        self.assertEqual(qs.as_json(s), dict(window_minutes=10080, delta_percent=3.0, lower_percent=3.0, upper_percent=3.0, label='observed', before=10.0, after=13.0, shared_with=0))

    def test_zero_delta_is_less_than_one_percent(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=10))]
        s = shares(recs)[1][K1]
        self.assertEqual(s['observed']['delta'], 0.0)
        self.assertEqual(qs.text(s), '< 1% of weekly Codex limit')
        self.assertNotIn('.', qs.percent_text(s))

    def test_before_comes_from_any_session(self):
        recs = [req('0', iso(0), turn='t0', session='other', q=quota(week=40)), req('a', iso(10), q=quota(week=42))]
        self.assertEqual(shares(recs)[1][K1]['observed']['before'], 40.0)

    def test_turns_over_the_same_interval_each_have_the_whole_movement_as_their_range(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(10), turn='t2', session='s2', out=3_000_000, q=quota(week=12)),
                req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18)),
                req('b2', iso(12), turn='t2', session='s2', out=3_000_000, q=quota(week=18))]
        got = shares(recs)[1]
        self.assertEqual((got[K1]['label'], got[K1]['observed']['shared_with'], got[K2]['observed']['shared_with']), ('range', 1, 1))
        self.assertEqual((qs.bounds(got[K1]), qs.bounds(got[K2])), ((0.0, 8.0), (0.0, 8.0)))  # nobody was alone: any split of the 8 fits
        self.assertIsNone(got[K1]['estimate'])  # eight points wide is no place for a point
        self.assertEqual(qs.text(got[K2]), '< 1%–8% of weekly Codex limit (shared with 1 turn)')
        self.assertIsNone(qs.as_json(got[K2])['delta_percent'])

    def test_a_narrow_range_also_shows_a_point_from_the_cost_in_each_step(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(11), turn='t2', session='s2', out=1_000_000, q=quota(week=13)),
                req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=14))]
        got = shares(recs)[1]
        a, b = got[K1], got[K2]
        self.assertEqual((a['label'], qs.bounds(a), a['estimate']), ('estimate', (2.0, 4.0), 2.0))  # alone for 1 + 1; the shared 2 weigh what each had in the step:
        self.assertEqual((b['label'], qs.bounds(b), b['estimate']), ('estimate', (0.0, 2.0), 2.0))  # only b had a request in it
        self.assertEqual(qs.text(a), '≈2% (2–4%) of weekly Codex limit (shared with 1 turn)')
        self.assertEqual(qs.text(b), '≈2% (< 1%–2%) of weekly Codex limit (shared with 1 turn)')
        self.assertAlmostEqual(a['estimate'] + b['estimate'], 4.0)  # the movement, once

    def test_the_bounds_conserve_the_movement_over_many_participants(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=12)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, q=quota(week=15)),
                req('a2', iso(11), turn='t1', session='s1', out=1_000_000, q=quota(week=16)),
                req('c1', iso(12), turn='t3', session='s3', out=1_000_000, q=quota(week=21)),
                req('b2', iso(13), turn='t2', session='s2', out=1_000_000, q=quota(week=22))]
        got = shares(sorted(recs, key=lambda r: r['ts']))[1]
        turns = [K1, K2, ('codex', 's3', 't3')]
        movement = 22.0 - 10.0
        self.assertLessEqual(sum(got[k]['lower'] for k in turns), movement + 1e-9)
        self.assertGreaterEqual(sum(got[k]['upper'] for k in turns), movement - 1e-9)
        for k in turns:
            self.assertLessEqual(got[k]['lower'], got[k]['upper'])
            if got[k]['estimate'] is not None:  # a point is one of the splits: inside the bounds
                self.assertTrue(got[k]['lower'] - 1e-9 <= got[k]['estimate'] <= got[k]['upper'] + 1e-9)

    def test_staggered_turns_overlap_so_they_have_ranges(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(11), turn='t2', session='s2', out=1_000_000, q=quota(week=17)),
                req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18))]
        got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
        a, b = got[K1], got[K2]
        self.assertEqual((a['label'], b['label']), ('range', 'range'))  # b ran inside a's span
        self.assertEqual((a['observed']['shared_with'], b['observed']['shared_with']), (1, 1))  # symmetric
        self.assertEqual((qs.bounds(a), qs.bounds(b)), ((2.0, 8.0), (0.0, 6.0)))  # a was alone for 1 + 1, b only in the step of 6
        self.assertLessEqual(a['lower'] + b['lower'], 8.0)
        self.assertGreaterEqual(a['upper'] + b['upper'], 8.0)

    def test_a_millisecond_shift_does_not_flip_the_label(self):
        def build(shift):
            return [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                    req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                    req('b1', iso(10, shift), turn='t2', session='s2', out=3_000_000, q=quota(week=12)),
                    req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18)),
                    req('b2', iso(12), turn='t2', session='s2', out=3_000_000, q=quota(week=18))]
        for shift in (0, 0.001, 0.5):
            recs = build(shift)
            got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
            self.assertEqual((got[K1]['label'], got[K2]['label']), ('range', 'range'), shift)
            self.assertLessEqual(got[K1]['lower'] + got[K2]['lower'], 8.0)
            self.assertGreaterEqual(got[K1]['upper'] + got[K2]['upper'], 8.0)

    def test_a_step_shared_by_two_turns_is_split_by_cost_and_conserved(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=12)),
                req('b1', iso(10), turn='t2', session='s2', out=1_000_000, q=quota(week=12)),
                req('a2', iso(11), turn='t1', session='s1', out=1_000_000, q=quota(week=16))]
        got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
        a, b = got[K1], got[K2]
        self.assertEqual((a['label'], b['label']), ('estimate', 'estimate'))
        self.assertEqual((qs.bounds(a), qs.bounds(b)), ((4.0, 6.0), (0.0, 2.0)))  # a was alone for the last 4
        self.assertAlmostEqual(a['estimate'] + b['estimate'], 6.0)  # 2 shared (1 + 1) and 4 alone for a: all of the 6, once
        self.assertAlmostEqual(b['estimate'], 1.0)

    def test_decrease_then_recovery_inside_a_turn_is_unknown(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(10), q=quota(week=11)),
                req('b', iso(11), q=quota(week=2)), req('c', iso(12), q=quota(week=12))]
        self.assertEqual(shares(recs)[1][K1]['label'], 'unknown')

    def test_a_stale_lower_reading_is_not_a_reset(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(10), q=quota(week=12)),
                req('b', iso(11), q=quota(week=11)), req('c', iso(12), q=quota(week=14))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['label'], s['observed']['delta'], s['observed']['after']), ('observed', 4.0, 14.0))

    def orchestration(self, other_account=False):
        """A parent turn with three parallel subagent sessions whose counters arrive interleaved and out of order (10 -> 18 overall)."""
        sub = lambda n: dict(session=f'sub{n}', turn=None, thread_kind='subagent', parent_session='s1')
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('m1', iso(10), out=1_000_000, q=quota(week=11)),
                req('a1', iso(11), out=1_000_000, q=quota(week=12), **sub(1)),
                req('b1', iso(12), out=1_000_000, q=quota(week=11), **sub(2)),  # stale
                req('c1', iso(13), out=1_000_000, q=quota(week=14), **sub(3)),
                req('m2', iso(14), out=1_000_000, q=quota(week=13)),  # stale
                req('a2', iso(15), out=1_000_000, q=quota(week=16), **sub(1)),
                req('b2', iso(16), out=1_000_000, q=quota(week=15), **sub(2)),  # stale
                req('m3', iso(17), out=1_000_000, q=quota(week=18))]
        if other_account:  # another account's counter (same limit id, plan and reset time) near its own limit
            recs += [req(f'x{i}', iso(10 + i, 30), turn='t9', session='s9', out=1000, q=quota(week=98 + i % 2)) for i in range(20)]
        return sorted(recs, key=lambda r: r['ts'])

    def test_a_parent_turn_gets_the_movement_its_subagents_cause(self):
        recs = self.orchestration()
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        parent = got[K1][10080]
        self.assertEqual((parent['label'], parent['observed']['delta']), ('observed', 8.0))
        self.assertEqual(sum(x[10080]['observed']['delta'] for k, x in got.items() if x[10080]['observed']), 8.0)  # conserved

    def test_an_interleaved_second_counter_does_not_take_the_movement(self):
        recs = self.orchestration(other_account=True)
        snaps = qs.snapshots_from_records(recs)
        got = qs.turn_shares(recs, snaps, TABLE)
        self.assertEqual((got[K1][10080]['label'], got[K1][10080]['observed']['delta']), ('observed', 8.0))
        self.assertEqual(len(qs.windows(snaps)), 2)  # two counters, two windows

    def test_a_lone_jump_in_one_session_stays_on_the_same_counter(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(1), turn='t0', q=quota(week=10)),
                req('b', iso(20), turn='t1', q=quota(week=25)), req('c', iso(21), turn='t1', q=quota(week=26))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        s = got[('codex', 's1', 't1')][10080]
        self.assertEqual((s['label'], s['observed']['delta']), ('observed', 16.0))

    def test_sequential_sessions_with_a_jump_share_a_counter(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(1), turn='t0', session='s0', q=quota(week=11)),
                req('b', iso(60), turn='t1', session='s1', q=quota(week=30)), req('c', iso(61), turn='t1', session='s1', q=quota(week=31))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)
        self.assertEqual(got[('codex', 's1', 't1')][10080]['observed']['delta'], 20.0)  # 11 -> 31

    def test_window_lower_bound_for_incomplete_counters(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000, complete=False)]
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=lambda r: prompts._cost(r, TABLE))[0]
        self.assertTrue(w['lower_bound'])

    def test_unattributed_usage_takes_its_movement(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1000, q=quota(week=11)),
                req('u1', iso(11), turn=None, session='su', out=10_000_000, q=quota(week=16)),  # no turn owns this session
                req('a2', iso(12), out=1000, q=quota(week=17))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'estimate')
        self.assertTrue(2.0 <= a['estimate'] < 2.1, a['estimate'])  # 1 + 1 alone, almost nothing of the unattributed 5
        self.assertNotIn(('codex', 'su', None), got)  # a pseudo-turn is never returned

    def test_a_sequential_jump_inside_the_conflict_interval_stays_on_one_counter(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10))]
        recs += [req(f'a{i}', iso(i), turn='t0', session='s0', q=quota(week=10)) for i in range(1, 6)]
        recs += [req('b1', iso(6), turn='t1', session='s1', q=quota(week=25)), req('b2', iso(7), turn='t1', session='s1', q=quota(week=25))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)[('codex', 's1', 't1')][10080]
        self.assertEqual((got['label'], got['observed']['delta']), ('observed', 15.0))

    def test_requests_without_a_snapshot_count_for_endpoints_and_competitors(self):
        end = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('x1', iso(10), q=quota(week=11)),
               req('x2', iso(12), q=quota(week=14)), req('x3', iso(14))]  # the last request has no snapshot
        s = qs.turn_shares(end, qs.snapshots_from_records(end), TABLE)[K1][10080]
        self.assertEqual((s['label'], s['lower'], s['upper']), ('range', 4.0, None))  # alone in every step, but the last request has no reading: at least 4
        self.assertEqual(qs.text(s), '≥4% of weekly Codex limit')
        gap = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a1', iso(10), out=1_000_000, q=quota(week=11)),
               req('c1', iso(12), turn='t2', session='s2', out=2_000_000),  # a competitor that reports no quota
               req('a2', iso(14), out=1_000_000, q=quota(week=19))]
        got = qs.turn_shares(gap, qs.snapshots_from_records(gap), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'range')
        self.assertEqual(a['observed']['shared_with'], 1)
        self.assertTrue(a['lower'] < a['upper'], (a['lower'], a['upper']))  # the competitor may have moved part of the 7 points
        self.assertNotIn(K2, got)  # nothing to show for it

    def test_a_sliding_reset_time_is_one_window_instance(self):
        slide = lambda n: (datetime.fromisoformat(RESET) + timedelta(minutes=20 * n)).isoformat()
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a1', iso(10), q=quota(week=11, resets=slide(1))),
                req('a2', iso(20), q=quota(week=12, resets=slide(2)))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)[K1][10080]
        self.assertEqual((got['label'], got['observed']['delta']), ('observed', 2.0))
        self.assertEqual(qs.windows(snaps)[0]['resets_at'], slide(2))  # the latest, as context

    def test_zero_of_one_limit_does_not_hide_an_unknown_one(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=10, five=20))]
        per = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1]
        per[300] = dict(per[300], observed=None, estimate=None, label='unknown')
        self.assertEqual(qs.largest({K1: per})[K1]['label'], 'unknown')

    def test_sessions_converging_after_a_reset_are_two_windows_and_no_split(self):
        def at(minutes, seconds=0):
            return iso(minutes, seconds)
        recs = [req('x0', at(0), turn='tx0', session='sx', q=quota(week=97)), req('y0', at(1), turn='ty0', session='sy', q=quota(week=98)),
                req('x1', at(2), turn='tx0', session='sx', q=quota(week=98)), req('y1', at(3), turn='ty0', session='sy', q=quota(week=98)),
                req('y2', at(10), turn='ty1', session='sy', q=quota(week=1)),
                req('x2', at(10, 10), turn='tx1', session='sx', q=quota(week=99)),  # a slower session still on the old counter
                req('y3', at(10, 20), turn='ty1', session='sy', q=quota(week=2)),
                req('x3', at(10, 30), turn='tx1', session='sx', q=quota(week=99)),
                req('x4', at(13), turn='tx1', session='sx', q=quota(week=3)), req('y4', at(14), turn='ty1', session='sy', q=quota(week=4))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual({s['key'][5] for s in snaps}, {0})  # the stragglers were no evidence of a second account
        self.assertEqual(len(qs.windows(snaps)), 2)
        self.assertEqual([s['key'][6] for s in snaps if s['ts'] in (at(10, 10), at(10, 30))], [0, 0])  # they stay with the old instance

    def test_a_low_usage_reset_is_a_new_window_even_with_a_small_drop(self):
        reset = (datetime.fromisoformat(RESET5) + timedelta(minutes=300)).isoformat()
        recs = [req('0', iso(0), turn='t0', q=quota(five=4)), req('0b', iso(120), turn='t0', q=quota(five=4)),
                req('a', iso(200), q=quota(five=1, resets5=reset)), req('b', iso(210), q=quota(five=2, resets5=reset))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len(qs.windows(snaps)), 2)
        got = qs.turn_shares(recs, snaps, TABLE)[K1][300]
        self.assertEqual((got['label'], got['observed']), ('unknown', None))  # not an observed 0% against the old window's 4%
        slide = lambda n: (datetime.fromisoformat(RESET5) + timedelta(minutes=2 * n)).isoformat()
        drift = [req('0', iso(0), turn='t0', q=quota(five=4)), req('a', iso(10), q=quota(five=3, resets5=slide(1))), req('b', iso(20), q=quota(five=4, resets5=slide(2)))]
        self.assertEqual(len(qs.windows(qs.snapshots_from_records(drift))), 1)  # drift of minutes is not a reset

    def test_window_cost_includes_quota_less_requests_of_reporting_sessions(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), out=1_000_000),  # same session, no snapshot
                req('c', iso(3), q=quota(week=12), out=1_000_000),
                req('d', iso(2, 30), turn='t9', session='s9', out=1_000_000)]  # a session that never reported a window
        cost = lambda r: prompts._cost(r, TABLE)
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        one = cost(recs[0])
        self.assertAlmostEqual(w['cost'], 3 * one)  # a, b and c; not d
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))
        none = [recs[0], recs[1], recs[2]]
        w = qs.windows(qs.snapshots_from_records(none), records=none, cost_of=cost)[0]
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (0, False))

    def test_window_costs_add_up_across_a_reset_with_stragglers(self):
        def at(minutes, seconds=0):
            return iso(minutes, seconds)
        ra, rb = (T0 + timedelta(minutes=9)).isoformat(), (T0 + timedelta(minutes=9 + 10080)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('x0', at(0), turn='tx0', session='sx', q=quota(week=97, resets=ra), **one), req('y0', at(1), turn='ty0', session='sy', q=quota(week=98, resets=ra), **one),
                req('x1', at(2), turn='tx0', session='sx', q=quota(week=98, resets=ra), **one), req('y1', at(3), turn='ty0', session='sy', q=quota(week=98, resets=ra), **one),
                req('y2', at(10), turn='ty1', session='sy', q=quota(week=1, resets=rb), **one),
                req('x2', at(10, 10), turn='tx1', session='sx', q=quota(week=99, resets=ra), **one),  # a straggler of the old instance
                req('xq', at(10, 15), turn='tx1', session='sx', **one),  # no snapshot: placed by the time, in the new window
                req('y3', at(10, 20), turn='ty1', session='sy', q=quota(week=2, resets=rb), **one),
                req('x3', at(10, 30), turn='tx1', session='sx', q=quota(week=99, resets=ra), **one),
                req('x4', at(13), turn='tx1', session='sx', q=quota(week=3, resets=rb), **one), req('y4', at(14), turn='ty1', session='sy', q=quota(week=4, resets=rb), **one)]
        snaps = qs.snapshots_from_records(recs)
        cost = lambda r: prompts._cost(r, TABLE)
        ws = qs.windows(snaps, records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        self.assertAlmostEqual(sum(w['cost'] for w in ws), len(recs) * cost(recs[0]))  # every request in exactly one instance
        self.assertAlmostEqual(ws[0]['cost'], 6 * cost(recs[0]))  # the four before the reset and the two stragglers
        self.assertAlmostEqual(ws[1]['cost'], 5 * cost(recs[0]))

    def test_a_request_before_the_reset_stays_in_the_old_window_though_the_new_reading_is_nearer(self):
        cost = lambda r: prompts._cost(r, TABLE)
        old, new = (T0 + timedelta(hours=2)).isoformat(), (T0 + timedelta(hours=7)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('a', iso(0), q=quota(five=40, resets5=old), **one), req('b', iso(1), q=quota(five=41, resets5=old), **one),
                req('q', iso(105), **one),  # 11:45 on the clock of the example: before the old window's reset, closer to the new readings
                req('c', iso(180), q=quota(five=2, resets5=new), **one), req('d', iso(181), q=quota(five=3, resets5=new), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        c = cost(recs[0])
        self.assertAlmostEqual(ws[0]['cost'], 3 * c)
        self.assertAlmostEqual(ws[1]['cost'], 2 * c)

    def test_a_hit_that_learned_its_window_keeps_its_first_time_in_the_table(self):
        resets = (T0 + timedelta(hours=4)).isoformat()
        windowless = req('w', iso(1), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': 'rate_limit_reached', 'windows': []})
        full = req('f', iso(2), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': None,
                                   'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': 100.0, 'resets_at': resets}]})
        recs = [windowless, full]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['window_minutes'], h['at']) for h in hits], [(300, iso(1))])  # resolved to the 5-hour window, at the first time
        w = qs.windows(qs.snapshots_from_records(recs), hits=hits)[0]
        self.assertIs(w['hit'], True)

    def test_a_reset_rollover_is_a_new_window_whichever_way_the_counter_went(self):
        for before, after in ((1, 2), (1, 1), (4, 1)):
            reset = (datetime.fromisoformat(RESET5) + timedelta(minutes=70)).isoformat()
            soon = (T0 + timedelta(minutes=60)).isoformat()
            recs = [req('a', iso(0), turn='t0', q=quota(five=before, resets5=soon)), req('b', iso(70), q=quota(five=after, resets5=reset))]
            self.assertEqual(len(qs.windows(qs.snapshots_from_records(recs))), 2, (before, after))
        early = [req('a', iso(0), turn='t0', q=quota(five=1, resets5=(T0 + timedelta(minutes=60)).isoformat())),
                 req('b', iso(30), q=quota(five=2, resets5=(T0 + timedelta(minutes=370)).isoformat()))]  # before the old reset time: not yet
        self.assertEqual(len(qs.windows(qs.snapshots_from_records(early))), 1)

    def test_a_turn_that_only_reports_the_five_hour_window_competes_in_the_weekly_one(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, five=20)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11, five=21)),
                req('c1', iso(12), turn='t2', session='s2', out=1_000_000, q=quota(five=30)),  # no weekly reading
                req('c2', iso(15), turn='t2', session='s2', out=1_000_000, q=quota(five=31)),
                req('a2', iso(20), out=1_000_000, q=quota(week=19, five=32))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'range')
        self.assertEqual(a['observed']['shared_with'], 1)

    def test_the_window_table_takes_hits_from_limit_hits_and_counts_quota_events(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=98), out=1_000_000), req('a', iso(10), q=quota(week=99), out=1_000_000),
                req('b', iso(20), q=quota(week=100), out=1_000_000)]  # crosses to 100 % without a reached type
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([h['reached'] for h in hits], ['window_full'])
        snaps = qs.snapshots_from_records(recs)
        self.assertFalse(qs.windows(snaps)[0]['hit'])  # the readings alone name no reached limit
        self.assertTrue(qs.windows(snaps, hits=hits)[0]['hit'])
        event = req('e', iso(30), turn=None, session='se', out=0, q={**quota(week=100), 'status': 'event'})
        cost = lambda r: prompts._cost(r, TABLE)
        only = [recs[0], recs[1]]
        snaps = qs.snapshots_from_records(only, events=[event])
        w = qs.windows(snaps, records=only, cost_of=cost, hits=limits.limit_hits(only, [event], TABLE))[0]
        self.assertEqual((w['peak_percent'], w['snapshots'], w['hit']), (100.0, 3, True))  # the quota-only event shows in the table
        self.assertAlmostEqual(w['cost'], 2 * cost(recs[0]))  # but it is no request and no cost
        self.assertEqual(len(snaps), 2)

    def test_a_hit_belongs_to_its_own_limit_plan_and_counter(self):
        cost = lambda r: prompts._cost(r, TABLE)
        # two weekly limits of one account: one at 12 %, one reaching 100 %
        recs = [req('a', iso(1), session='sa', q=quota(week=12, limit_id='codex')),
                req('b', iso(2), session='sb', turn='tb', q=quota(week=100, limit_id='codex_x', reached='rate_limit_reached'))]
        by = {w['account']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs), hits=limits.limit_hits(recs, [], TABLE))}
        self.assertEqual(by, {'codex': False, 'codex_x': True})
        # two plans
        recs = [req('a', iso(1), session='sa', q=quota(week=12, plan='pro')),
                req('b', iso(2), session='sb', turn='tb', q=quota(week=100, plan='team', reached='rate_limit_reached'))]
        by = {w['plan_type']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs), hits=limits.limit_hits(recs, [], TABLE))}
        self.assertEqual(by, {'pro': False, 'team': True})
        # two counters of one limit and plan (the interleaved second account is the one that reaches 100 %)
        recs = self.orchestration(other_account=True)
        for r in recs:
            if r['session'] == 's9':
                r['quota'] = quota(week=100, reached='rate_limit_reached')
        snaps = qs.snapshots_from_records(recs)
        hits = limits.limit_hits(recs, [], TABLE)
        ws = qs.windows(snaps, hits=hits)
        self.assertEqual(sorted(w['hit'] for w in ws), [False, True])
        self.assertTrue(next(w for w in ws if w['peak_percent'] >= 100)['hit'])

    def test_every_request_of_a_reporting_session_is_in_one_instance(self):
        cost = lambda r: prompts._cost(r, TABLE)
        one = dict(out=1_000_000)
        # the edge requests (before the first and after the last reading) belong to the instance too
        recs = [req(str(n), iso(n), **one, **({'q': quota(week=10 + n)} if n in (2, 4) else {})) for n in range(1, 6)]
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        self.assertAlmostEqual(w['cost'], 5 * cost(recs[0]))
        self.assertFalse(w['lower_bound'])
        # between two instances: the nearer in time; and every request once
        first, reset = (T0 + timedelta(minutes=1000)).isoformat(), (T0 + timedelta(minutes=1000 + 10080)).isoformat()
        recs = [req('a', iso(0), q=quota(week=10, resets=first), **one), req('b', iso(100), q=quota(week=50, resets=first), **one), req('m1', iso(200), **one),
                req('m2', iso(2000), **one), req('c', iso(2200), q=quota(week=2, resets=reset), **one), req('d', iso(2300), q=quota(week=3, resets=reset), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        self.assertAlmostEqual(ws[0]['cost'], 3 * cost(recs[0]))  # a, b, m1 (nearer to b) ...
        self.assertAlmostEqual(ws[1]['cost'], 3 * cost(recs[0]))  # ... m2 (nearer to c), c, d
        self.assertAlmostEqual(sum(w['cost'] for w in ws), len(recs) * cost(recs[0]))  # six requests, each in exactly one instance
        # a request too far from any reading of its session (beyond the window length) cannot be placed
        far = [req('a', iso(0), q=quota(five=10), **one), req('b', iso(10), q=quota(five=11), **one), req('x', iso(500), **one)]
        w = qs.windows(qs.snapshots_from_records(far), records=far, cost_of=cost)[0]
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))
        self.assertAlmostEqual(w['cost'], 2 * cost(far[0]))

    def test_a_turn_whose_readings_were_lost_after_a_reset_still_competes(self):
        reset = (datetime.fromisoformat(RESET) + timedelta(days=7)).isoformat()
        recs = [req('b0', iso(0), turn='tb', session='sb', q=quota(week=80)), req('b1', iso(10), turn='tb', session='sb', q=quota(week=81)),
                req('z', iso(19), turn='tz', session='sz', q=quota(week=1, resets=reset)),  # the first reading of the new instance
                req('a0', iso(20), turn='ta', session='sa', q=quota(week=1, resets=reset)), req('a1', iso(21), turn='ta', session='sa', q=quota(week=2, resets=reset)),
                req('a2', iso(25), turn='ta', session='sa', q=quota(week=5, resets=reset)), req('a3', iso(31), turn='ta', session='sa', q=quota(week=8, resets=reset))]
        recs += [req(f'q{n}', iso(22 + n), turn='tb', session='sb', out=2_000_000) for n in range(8)]  # the same turn went on, with no readings
        snaps = qs.snapshots_from_records(recs)
        a = qs.turn_shares(recs, snaps, TABLE)[('codex', 'sa', 'ta')][10080]
        self.assertEqual(a['label'], 'range')
        self.assertEqual(a['observed']['shared_with'], 1)
        self.assertTrue(a['lower'] < a['upper'] <= 7.0)  # alone for part of the 7 points, never more than all of them

    def test_a_request_after_an_instances_reset_is_not_charged_to_it(self):
        cost = lambda r: prompts._cost(r, TABLE)
        reset_a = (T0 + timedelta(minutes=60)).isoformat()
        reset_b = (T0 + timedelta(minutes=60 + 10080)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('a', iso(0), q=quota(week=10, resets=reset_a), **one), req('b', iso(58), q=quota(week=50, resets=reset_a), **one),
                req('q', iso(61), **one),  # after the first window reset, 3 minutes after its last reading
                req('c', iso(100), q=quota(week=2, resets=reset_b), **one), req('d', iso(110), q=quota(week=3, resets=reset_b), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        c = cost(recs[0])
        self.assertAlmostEqual(ws[0]['cost'], 2 * c)
        self.assertAlmostEqual(ws[1]['cost'], 3 * c)
        # a request after the reset that no later instance covers cannot be placed
        late = [recs[0], recs[1], req('z', iso(61 + 20000), **one)]
        w = qs.windows(qs.snapshots_from_records(late), records=late, cost_of=cost)[0]
        self.assertAlmostEqual(w['cost'], 2 * c)
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))

    def test_an_unpriced_concurrent_turn_is_weighed_by_its_tokens_at_the_average_price(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=13)),
                req('a2', iso(12), out=1_000_000, q=quota(week=15)), req('b2', iso(12, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=16))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a, b = got[K1][10080], got[K2][10080]
        self.assertEqual((a['label'], b['label']), ('estimate', 'estimate'))
        self.assertGreater(b['estimate'], 1.0)  # not weighed as free
        self.assertAlmostEqual(a['estimate'] + b['estimate'], 6.0)  # the movement 10 -> 16, once

    def test_with_no_priced_request_in_the_window_the_bounds_still_hold_but_there_is_no_point(self):
        recs = [req('0', iso(0), turn='t0', session='s0', model='no-such-model', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, model='no-such-model', q=quota(week=11)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=13)),
                req('a2', iso(12), out=1_000_000, model='no-such-model', q=quota(week=15)), req('b2', iso(12, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=16))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        self.assertEqual((got[K1][10080]['label'], got[K2][10080]['label']), ('range', 'range'))  # no price is needed for the bounds; with no rate there is no point
        self.assertIsNone(got[K1][10080]['estimate'])

    def test_a_lone_unpriced_turn_is_observed(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, model='no-such-model', q=quota(week=11)),
                req('a2', iso(12), out=1_000_000, model='no-such-model', q=quota(week=15))]
        a = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1][10080]
        self.assertEqual((a['label'], a['observed']['delta']), ('observed', 5.0))

    def test_a_five_hour_only_session_counts_in_the_weekly_window_cost(self):
        recs = [req('0', iso(0), turn='t0', session='s0', out=1_000_000, q=quota(week=10, five=20)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11, five=21)),
                req('c1', iso(12), turn='t2', session='s2', out=1_000_000, q=quota(five=30)),  # no weekly reading
                req('c2', iso(15), turn='t2', session='s2', out=1_000_000, q=quota(five=31)),
                req('a2', iso(20), out=1_000_000, q=quota(week=19, five=32))]
        cost = lambda r: prompts._cost(r, TABLE)
        ws = {w['minutes']: w for w in qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)}
        self.assertAlmostEqual(ws[10080]['cost'], 5 * cost(recs[0]))
        self.assertFalse(ws[10080]['lower_bound'])

    def test_the_shown_window_prefers_what_the_turn_surely_used(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, five=20)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11, five=21)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, q=quota(week=11, five=21)),  # shares the weekly step only
                req('a2', iso(11), turn='t1', session='s1', out=1_000_000, q=quota(week=11, five=22))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        shown = qs.largest(got)[K1]
        self.assertGreaterEqual(shown['lower'], 1.0)

    def test_a_missing_trailing_reading_leaves_only_a_lower_bound(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(11), turn='t2', session='s2', out=1_000_000, q=quota(week=17)),
                req('b2', iso(12), turn='t2', session='s2', out=1_000_000, q=quota(week=17)),
                req('a2', iso(12, 30), turn='t1', session='s1', out=1_000_000)]  # a request of A after its last reading
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a, b = got[K1][10080], got[K2][10080]
        self.assertEqual((a['label'], a['lower'], a['upper']), ('range', 1.0, None))  # it could have moved more than the shared 6
        self.assertEqual((qs.bounds(a), qs.bounds(b)), ((1.0, None), (0.0, 6.0)))
        self.assertEqual(qs.text(a), '≥1% of weekly Codex limit (shared with 1 turn)')
        j = qs.as_json(a)
        self.assertEqual((j['lower_percent'], j['upper_percent'], j['delta_percent']), (1.0, None, None))
        # and with nothing proven (lower 0) there is nothing to show
        none = [recs[0], recs[2], recs[3], req('a1', iso(10), turn='t1', session='s1', out=1_000_000)]
        none = sorted(none, key=lambda r: r['ts'])
        self.assertEqual(qs.turn_shares(none, qs.snapshots_from_records(none), TABLE).get(K1, {}).get(10080, {'label': 'unknown'})['label'], 'unknown')

    def test_per_window_shares_are_kept(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=12, five=26))]
        per = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1]
        self.assertEqual({m: x['observed']['delta'] for m, x in per.items()}, {10080: 2.0, 300: 6.0})
        self.assertEqual(qs.largest({K1: per})[K1]['window_key'][2], 300)

    def test_reset_inside_a_turn_is_not_observed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=60)), req('a', iso(10), q=quota(week=61)), req('b', iso(11), q=quota(week=3))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['observed'], s['estimate'], s['label']), (None, None, 'unknown'))
        self.assertEqual(qs.text(s), 'share of weekly Codex limit: n/a')

    def test_a_turn_crossing_into_a_new_window_instance_is_not_observed(self):
        new = (T0 + timedelta(days=10)).isoformat()
        recs = [req('0', iso(0), turn='t0', q=quota(week=60)), req('a', iso(10), q=quota(week=61)), req('b', iso(11), q=quota(week=2, resets=new))]
        self.assertEqual(shares(recs)[1][K1]['label'], 'unknown')

    def test_no_snapshot_before_the_turn_is_unknown(self):
        recs = [req('a', iso(10), q=quota(week=11)), req('b', iso(11), q=quota(week=13))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['observed'], s['label']), (None, 'unknown'))
        self.assertNotIn('0%', qs.text(s))

    def test_turn_spanning_both_windows_picks_the_largest_delta(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=11, five=26))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['window_key'][2], s['observed']['delta']), (300, 6.0))
        self.assertEqual(qs.window_name(300), '5-hour')

    def test_only_limits_the_turns_computed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=12))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(set(qs.turn_shares(recs, snaps, TABLE, only={K1})), {K1})
        self.assertEqual(qs.turn_shares(recs, snaps, TABLE, only=set()), {})

    def test_tie_prefers_the_weekly_window(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=12, five=22))]
        self.assertEqual(shares(recs)[1][K1]['window_key'][2], 10080)

    def test_credits_depleted_and_rejected_events(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=12, reached='workspace_owner_credits_depleted')),
                req('r', iso(11), q={**quota(week=100), 'status': 'rejected'})]
        snaps, got = shares(recs)
        self.assertEqual(len(snaps), 2)
        self.assertEqual(got[K1]['observed']['delta'], 2.0)
        self.assertFalse(qs.windows(snaps)[0]['hit'])

    def test_unwindowed_and_quota_free_records_make_no_share(self):
        self.assertEqual(qs.compute([req('a', iso(1))], TABLE), ([], {}))

    def test_performance_shape_many_records(self):
        recs = [req(str(i), iso(i), turn=f't{i // 5}', q=quota(week=i // 50)) for i in range(2000)]
        _, got = shares(recs)
        self.assertEqual(len(got), 400)



def history_records():
    """Codex observations as History would return them: a first request, then two turns, the second one expensive."""
    return [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
            req('a', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=12)),
            req('b', iso(60), turn='t2', session='s2', out=2_000_000, q=quota(week=21))]


class TestOrchestration:
    @staticmethod
    def build():
        return Shares('test_per_window_shares_are_kept').orchestration()


class Surfaces(unittest.TestCase):
    def build(self, **kw):
        from tokenatlas import report
        recs = history_records()
        return report.build_report(recs, {}, quota=kw.pop('with_quota', True), now=datetime(2026, 9, 5, tzinfo=timezone.utc), **kw)

    def test_report_payload_with_and_without_snapshots(self):
        payload = self.build()
        self.assertEqual(sorted(v['percent'] for v in payload['quota_shares'].values() if v['percent'] is not None), [2.0, 9.0])
        self.assertEqual(len(payload['quota_windows']), 1)
        self.assertEqual(payload['quota_windows'][0]['peak_percent'], 21.0)
        bare = self.build(with_quota=False)
        self.assertNotIn('quota_shares', bare)
        self.assertNotIn('quota_windows', bare)

    def test_strings_in_both_languages_and_section_markup(self):
        import base64, gzip, json, re
        from tokenatlas import report
        page = report.render_report(self.build())
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        self.assertEqual(i18n['sv']['qs_est'], '≈ {n} % av {w} (uppskattning)')
        self.assertEqual(i18n['sv']['qs_obs'], '~{n} % av {w}')
        self.assertEqual(i18n['sv']['qs_w_week'], 'veckogränsen för {agent}')
        self.assertEqual(i18n['en']['qs_est'], '≈ {n}% of {w} (estimate)')
        self.assertEqual(i18n['en']['qw_title'], 'Limit windows')
        self.assertIn('id="quota-windows" class="hidden"', page)

    def test_every_quota_share_string_names_the_agent_the_same_way(self):  # #115
        import re
        texts = json.loads((Path(__file__).parent / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        for lang, (five, week, other) in {'en': ('the 5-hour {agent} limit', 'the weekly {agent} limit', 'the {n}-minute {agent} limit'),
                                          'sv': ('5-timmarsgränsen för {agent}', 'veckogränsen för {agent}', '{n}-minutersgränsen för {agent}')}.items():
            t = texts[lang]
            self.assertEqual((t['qs_w_5h'], t['qs_w_week'], t['qs_w_other']), (five, week, other))
            self.assertFalse([k for k in t if re.match(r'qs_w_\w+_(claude|codex)$|glance_w_', k)], lang)  # no agent-specific or unnamed variants
            quota_keys = [k for k in t if k.startswith('qs_') and '{w}' in t[k] or k.startswith('glance_qs')]
            self.assertTrue(quota_keys)
            for k in quota_keys:  # a share is of a named window ({w} = "the weekly Claude limit"), never of a bare "limit"
                self.assertIn('{w}', t[k], (lang, k))
                self.assertNotIn('{agent}', t[k], (lang, k))
                self.assertNotRegex(t[k], r'(weekly|5-hour|veckogräns|5-timmarsgräns)', (lang, k))
                self.assertNotIn('Claude Code', t[k], (lang, k))

    def test_shared_report_has_no_ids_or_text(self):
        import json
        payload = self.build(redact=True)
        blob = json.dumps({k: payload[k] for k in ('quota_shares', 'quota_windows')})
        for secret in ('s1', 's2', 't1', 't2', 'codex"', 'pro'):
            self.assertNotIn(f'"{secret}"', blob)
        self.assertEqual(set(payload['quota_windows'][0]), {'harness', 'account', 'minutes', 'resets_at', 'start', 'peak_percent', 'peak_at', 'hit',
                                                            'snapshots', 'cost', 'unpriced_requests', 'uncertain_requests', 'lower_bound'})
        self.assertEqual(set(next(iter(payload['quota_shares'].values()))), {'harness', 'minutes', 'label', 'percent', 'lower', 'upper', 'shared_with'})

    def test_a_subagent_only_filter_keeps_the_quota_fact_equal_to_the_card(self):
        from tokenatlas import report
        recs = TestOrchestration.build()
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        subs = [r for r in recs if r['thread_kind'] == 'subagent']
        payload = report.build_report(subs, {}, universe=recs, now=now)
        fact = next(f for f in payload['insights']['windows'][1]['facts'] if f['id'] == 'quota_share')
        self.assertEqual([x['percent'] for x in fact['values']['turns']], [8.0])
        self.assertEqual([v['percent'] for v in payload['quota_shares'].values()], [8.0])

    def test_shared_report_pseudonymizes_a_non_default_limit_id(self):
        import json
        from tokenatlas import report
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, limit_id='customer-12345')), req('a', iso(10), q=quota(week=12, limit_id='customer-12345'))]
        shared = report.build_report(recs, {}, redact=True, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertNotIn('customer-12345', json.dumps(shared))
        self.assertEqual(shared['quota_windows'][0]['account'], 'limit 001')
        private = report.build_report(recs, {}, redact=False, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(private['quota_windows'][0]['account'], 'customer-12345')

    def test_filtered_report_keeps_the_whole_history_shares(self):
        from tokenatlas import report
        recs = history_records()
        for r in recs:
            r['project_id'], r['project_label'] = ('p-' + r['turn_id']), r['turn_id']
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        full = report.build_report(recs, {}, now=now)
        sub = [r for r in recs if r['turn_id'] == 't2']  # a project filter: the baselines and competitors are gone
        part = report.build_report(sub, {}, universe=recs, now=now)
        self.assertEqual([v['percent'] for v in part['quota_shares'].values()], [9.0])
        self.assertEqual(part['quota_windows'], full['quota_windows'])
        alone = report.build_report(sub, {}, now=now)
        self.assertNotEqual([v['label'] for v in alone['quota_shares'].values()], ['observed'])  # without the universe it cannot know

    def test_range_strings_in_both_languages(self):
        import json
        from pathlib import Path
        texts = json.loads((Path(__file__).parent / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        sv, en = texts['sv'], texts['en']
        self.assertEqual((en['qs_rng'], sv['qs_rng']), ('{lo}–{up}%', '{lo}–{up} %'))
        self.assertEqual((en['qs_rng_lt1'], sv['qs_rng_lt1']), ('< 1%–{up}%', '< 1 %–{up} %'))
        self.assertEqual((en['qs_range'], sv['qs_range']), ('{r} of {w}{sh}', '{r} av {w}{sh}'))
        self.assertEqual((en['qs_atleast'], sv['qs_atleast']), ('≥ {n}%', '≥ {n} %'))
        self.assertEqual((en['qs_pt_short'], sv['qs_pt_short']), ('≈ {n}% ({r})', '≈ {n} % ({r})'))
        self.assertEqual((en['qs_with'], en['qs_with_one']), (' (shared with {n} turns)', ' (shared with 1 turn)'))
        self.assertEqual((sv['qs_with'], sv['qs_with_one']), (' (delad med {n} turer)', ' (delad med 1 tur)'))
        self.assertEqual(en['qs_w_week'].format(agent='Codex'), 'the weekly Codex limit')
        self.assertEqual(sv['qs_w_week'].format(agent='Codex'), 'veckogränsen för Codex')

    def test_top_text_and_json_for_a_range(self):
        item = dict(window_minutes=10080, delta_percent=None, lower_percent=2.0, upper_percent=28.0, label='range', before=10.0, after=40.0, shared_with=4)
        self.assertEqual(qs.line(item, 'codex'), '2–28% of weekly Codex limit (shared with 4 turns)')
        self.assertEqual(qs.line({**item, 'lower_percent': 0.2, 'shared_with': 1}, 'codex'), '< 1%–28% of weekly Codex limit (shared with 1 turn)')
        self.assertEqual(qs.line({**item, 'label': 'estimate', 'delta_percent': 9.0, 'lower_percent': 7.0, 'upper_percent': 11.0}, 'codex'),
                         '≈9% (7–11%) of weekly Codex limit (shared with 4 turns)')
        self.assertEqual(qs.range_text(0.0, 0.2), '< 1%')
        self.assertEqual(qs.range_text(4.0, 4.0), '4%')

    def test_one_rule_decides_whether_it_is_a_range_from_the_rounded_endpoints(self):
        from tokenatlas import insights
        for lo, up, shown in ((2.0, 2.5, '2–3%'), (1.4, 1.6, '1–2%'), (2.0, 2.4, '2–3%'), (0.2, 0.4, '< 1%'), (3.0, 3.0000000001, '3%'), (0.6, 3.6, '< 1%–4%')):
            self.assertEqual(qs.range_text(lo, up), shown)
            if shown != '< 1%':  # (a range under 1 point reads "< 1%" whatever its ends)
                self.assertEqual(qs.wide(lo, up), '–' in shown)
        item = dict(window_minutes=10080, delta_percent=2.2, lower_percent=2.0, upper_percent=2.5, label='estimate', before=1, after=5, shared_with=1)
        self.assertEqual(qs.line(item, 'claude'), '≈2% (2–3%) of weekly Claude limit (shared with 1 turn)')  # a Claude reading with fractions
        self.assertEqual(qs.line({**item, 'upper_percent': 2.0}, 'claude'), '≈2% of weekly Claude limit (estimate)')
        turn = dict(cost=5.0, percent=2.2, lower=2.0, upper=2.5, label='estimate', harness='claude')
        self.assertEqual(insights._quota_pct(turn), '≈2% (2–3%)')
        self.assertEqual(insights._quota_pct({**turn, 'upper': 2.0}), '≈2%')

    def test_bounds_are_never_rounded_past_the_evidence(self):
        from tokenatlas import insights
        one = dict(window_minutes=10080, delta_percent=None, lower_percent=3.6, upper_percent=None, label='range', before=1, after=9, shared_with=1)
        self.assertEqual(qs.line(one, 'claude'), '≥3% of weekly Claude limit (shared with 1 turn)')  # floored, not "≥ 4%"
        self.assertEqual(qs.line({**one, 'lower_percent': 0.6}, 'claude'), 'share of weekly Claude limit: n/a')  # a floor of 0 is no information
        two = {**one, 'lower_percent': 3.6, 'upper_percent': 9.2}
        self.assertEqual(qs.line(two, 'claude'), '3–10% of weekly Claude limit (shared with 1 turn)')  # floor the lower end, ceil the upper
        turn = dict(cost=1.0, percent=None, lower=3.6, upper=None, label='range', harness='claude')
        self.assertEqual(insights._quota_pct(turn), '≥3%')
        self.assertEqual(insights._quota_pct({**turn, 'upper': 9.2}), '3–10%')
        share = dict(window_key=('claude', 'claude', 10080, None, None, 0, 0), observed=dict(before=0, after=4, delta=3.999, shared_with=0), estimate=None,
                     label='range', lower=3.999, upper=None)
        j = qs.as_json(share)
        self.assertEqual((j['lower_percent'], j['upper_percent']), (3.99, None))  # never 4.0
        j = qs.as_json({**share, 'upper': 4.001})
        self.assertEqual((j['lower_percent'], j['upper_percent']), (3.99, 4.01))

    def test_a_point_that_rounds_to_zero_is_left_out_of_a_narrow_range(self):
        from tokenatlas import insights
        item = dict(window_minutes=10080, delta_percent=0.3, lower_percent=0.0, upper_percent=2.0, label='estimate', before=1, after=3, shared_with=4)
        self.assertEqual(qs.line(item, 'codex'), '< 1%–2% of weekly Codex limit (shared with 4 turns)')  # not "≈ 0% (< 1%–2%)"
        self.assertEqual(insights._quota_pct(dict(cost=1.0, percent=0.3, lower=0.0, upper=2.0, label='estimate', harness='codex')), '< 1%–2%')

    def test_the_insights_fact_lists_a_range_for_independent_overlapping_turns(self):
        from tokenatlas import insights
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=3_000_000, q=quota(week=14)),
                req('b1', iso(11), turn='t2', session='s2', out=2_000_000, q=quota(week=20)),
                req('a2', iso(14), turn='t1', session='s1', out=3_000_000, q=quota(week=22)),
                req('b2', iso(15), turn='t2', session='s2', out=2_000_000, q=quota(week=26))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        self.assertEqual({got[K1][10080]['label'], got[K2][10080]['label']}, {'range'})
        result = insights.cost_facts(recs, TABLE, quota=got)
        fact = next(f for f in result['facts'] if f['id'] == 'quota_share')
        turns = {x['label']: x for x in fact['values']['turns']}
        self.assertEqual(set(turns), {'range'})
        for x in fact['values']['turns']:
            self.assertIsNone(x['percent'])
            self.assertLess(x['lower'], x['upper'])
        text = insights.render_text(result)
        self.assertRegex(text, r'used (< 1%|\d+)–\d+%( and (< 1%|\d+)–\d+%)? of the weekly Codex limit')

    def test_a_range_blocks_calibration_but_unknown_does_not(self):
        from tokenatlas import budget
        derived = {('codex', 10080, 'pro'): dict(harness='codex', minutes=10080, plan='pro', budget_usd=10.0, readings=3, spread=[9, 11], source='manual', date='2026-10-01')}
        items = [dict(harness='codex', session='s1', turn_id='t1', quota_share=dict(label='range', lower_percent=1.0, upper_percent=9.0)),
                 dict(harness='codex', session='s1', turn_id='t2', quota_share=dict(label='unknown'))]
        budget.mark_turns(items, derived, {('codex', 's1', 't1'): (5.0, True, False), ('codex', 's1', 't2'): (5.0, True, False)})
        self.assertEqual(items[0]['quota_share']['label'], 'range')

    def test_the_insights_fact_lists_ranges(self):
        from tokenatlas import insights
        recs = TestOrchestration.build()
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        result = insights.cost_facts(recs, TABLE, quota=got)
        fact = next(f for f in result['facts'] if f['id'] == 'quota_share')
        self.assertTrue(all('lower' in x and 'upper' in x for x in fact['values']['turns']))
        text = insights.render_text(result)
        self.assertRegex(text, r'costliest turns used .*% of the weekly Codex limit')

    def test_top_json_and_text(self):
        from tokenatlas import prompts
        recs = history_records()
        _, got = qs.compute(recs, TABLE)
        top = prompts.top_prompts(recs, TABLE, 5)['prompts']
        qs.mark_turns(top, got)
        by = {p['turn_id']: p['quota_share'] for p in top}
        self.assertEqual(by['t2'], dict(window_minutes=10080, delta_percent=9.0, lower_percent=9.0, upper_percent=9.0, label='observed', before=12.0, after=21.0, shared_with=0))
        self.assertEqual(qs.line(by['t2'], 'codex'), '~9% of weekly Codex limit')
        self.assertEqual(by['t0']['label'], 'unknown')
        self.assertEqual(qs.line(by['t0'], 'codex'), 'share of weekly Codex limit: n/a')
        qs.mark_turns([plain := {'harness': 'claude', 'session': 'x', 'turn_id': 'y'}], got)
        self.assertIsNone(plain['quota_share'])

    def test_insights_fact(self):
        from tokenatlas import insights
        recs = history_records()
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        fact = next(f for f in insights.cost_facts(recs, TABLE, quota=got)['facts'] if f['id'] == 'quota_share')
        self.assertEqual(([x['percent'] for x in fact['values']['turns']], fact['values']['observed_turns'], fact['provenance']), ([9.0, 2.0], 2, 'computed'))
        self.assertNotIn('total_percent', fact['values'])
        result = insights.cost_facts(recs, TABLE, quota=got)
        self.assertIn('your 2 costliest turns used ~9% of the weekly Codex limit and ~2% of the weekly Codex limit (each of its own window)', insights.render_text(result))
        self.assertFalse(any(f['id'] == 'quota_share' for f in insights.cost_facts(recs, TABLE)['facts']))
        self.assertFalse(any(f['id'] == 'quota_share' for f in insights.cost_facts(recs, TABLE, quota={})['facts']))

    def test_insights_uses_the_weekly_window_even_when_five_hour_moved_more(self):
        from tokenatlas import insights
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, five=20)), req('a', iso(10), out=1_000_000, q=quota(week=12, five=26))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        fact = next(f for f in insights.cost_facts(recs, TABLE, quota=got)['facts'] if f['id'] == 'quota_share')
        self.assertEqual([x['percent'] for x in fact['values']['turns']], [2.0])


def creq(id, ts, turn='c1', session='cs1', out=1000, **kw):
    """A Claude request: as req, from the Claude harness."""
    return dict(req(id, ts, turn=turn, session=session, out=out, **kw), harness='claude', provider='anthropic', model='claude-opus-4-8')


def cline_row(minutes, five=None, seven=None, session='cs1', resets=RESET, resets5=RESET5, seconds=0):
    return dict(ts=iso(minutes, seconds), session=session, five_hour=None if five is None else dict(used_percent=five, resets_at=resets5),
               seven_day=None if seven is None else dict(used_percent=seven, resets_at=resets))


def cline(*args, **kw):
    return json.dumps(cline_row(*args, **kw))


class ClaudeSnapshots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'claude-quota.jsonl'

    def write(self, *lines):
        self.path.write_text('\n'.join(lines) + '\n')

    def test_readings_map_to_the_turn_of_the_latest_request_of_the_session(self):
        recs = [creq('a', iso(0)), creq('b', iso(10), turn='c2'), creq('c', iso(11), turn='c2', session='other', parent_session=None)]
        self.write(cline(0, 5, 10), cline(1, 5, 10), cline(12, 7, 12), cline(30, 8, 13, session='nobody'))
        got = qs.claude_snapshots(self.path, recs, prompts.assign_prompts(recs))
        self.assertEqual([(s['window'][0], s['turn'], s['i'], s['at']) for s in got][:4],
                         [(300, ('claude', 'cs1', 'c1'), 0, prompts._t(iso(0))), (10080, ('claude', 'cs1', 'c1'), 0, prompts._t(iso(0))),
                          (300, ('claude', 'cs1', 'c1'), 0, prompts._t(iso(0))), (10080, ('claude', 'cs1', 'c1'), 0, prompts._t(iso(0)))])
        late = [s for s in got if s['at'] == prompts._t(iso(10))]  # the reading at minute 12 belongs to the request at minute 10
        self.assertEqual({s['turn'] for s in late}, {('claude', 'cs1', 'c2')})
        stray = [s for s in got if s['who'] == ('claude', 'nobody', None)]
        self.assertEqual(({s['turn'] for s in stray}, {s['i'] for s in stray}), ({None}, {None}))  # no request of that session: unattributed
        self.assertTrue(all((s['harness'], s['account'], s['plan_type']) == ('claude', 'claude', None) for s in got))

    def test_an_idle_session_reading_is_not_placed_at_an_old_request(self):
        recs = [creq('a', iso(0))]
        self.write(cline(60, 5, 10))
        got = qs.claude_snapshots(self.path, recs, prompts.assign_prompts(recs))
        self.assertEqual({(s['turn'], s['i'], s['t'], s['at']) for s in got}, {(None, None, prompts._t(iso(60)), None)})

    def test_a_claude_turn_alone_gets_an_observed_share(self):
        recs = [creq('a', iso(1)), creq('b', iso(5))]
        self.write(cline(0, 4, 10, session='cs0'), cline(2, 5, 11), cline(6, 7, 14))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        by = qs.turn_shares(recs, snaps, TABLE)
        week, five = by[('claude', 'cs1', 'c1')][10080], by[('claude', 'cs1', 'c1')][300]
        self.assertEqual((week['label'], qs.value(week), five['label'], qs.value(five)), ('observed', 4.0, 'observed', 3.0))
        self.assertEqual(qs.text(qs.largest(by)[('claude', 'cs1', 'c1')]), '~4% of weekly Claude limit')
        self.assertEqual(qs.line(qs.as_json(five), 'claude'), '~3% of 5-hour Claude limit')
        windows = qs.windows(snaps, records=recs, cost_of=lambda r: 1.0)
        self.assertEqual({(w['harness'], w['minutes'], w['peak_percent'], w['cost']) for w in windows}, {('claude', 300, 7.0, 2.0), ('claude', 10080, 14.0, 2.0)})

    def test_two_claude_sessions_share_a_step_and_estimate(self):
        recs = [creq('a', iso(0), turn='c1', session='s1', out=1_000_000), creq('b', iso(10), turn='c1', session='s1', out=1_000_000),
                creq('c', iso(0), turn='c2', session='s2', out=1_000_000), creq('d', iso(10), turn='c2', session='s2', out=1_000_000)]
        self.write(cline(-5, 0, 0, session='before'), cline(0, 1, 1, session='s1'), cline(0, 1, 1, session='s2'), cline(11, 5, 5, session='s1'), cline(11, 5, 5, session='s2'))
        by = qs.turn_shares(recs, qs.snapshots_from_records(recs, claude=self.path), TABLE)
        self.assertEqual([by[k][10080]['label'] for k in (('claude', 's1', 'c1'), ('claude', 's2', 'c2'))], ['estimate', 'estimate'])

    def test_malformed_lines_are_skipped_and_counted(self):
        self.write(cline(0, 5, 10), 'not json', '[1]', json.dumps({'ts': 'bad', 'five_hour': {'used_percent': 1, 'resets_at': RESET5}}),
                   json.dumps({'ts': iso(1), 'five_hour': {'used_percent': float('nan'), 'resets_at': RESET5}}),
                   json.dumps({'ts': iso(1), 'session': 3, 'five_hour': {'used_percent': 1, 'resets_at': RESET5}}), '', cline(2, 6, 11))
        rows, bad = qs.read_claude(self.path)
        self.assertEqual((len(rows), bad), (2, 5))

    def test_an_absent_file_changes_nothing(self):
        recs = history_records()
        missing = self.path.with_name('none.jsonl')
        self.assertEqual(qs.read_claude(missing), ([], 0))
        a, b = qs.snapshots_from_records(recs), qs.snapshots_from_records(recs, claude=missing)
        self.assertEqual((list(a), a.requests, a.bearing), (list(b), b.requests, b.bearing))
        self.assertEqual(qs.compute([creq('a', iso(0))], TABLE, claude=missing), ([], {}))
        self.assertIsNone(qs.claude_token(missing))

    def test_codex_and_claude_are_separate_series(self):
        recs = [*history_records(), creq('a', iso(1)), creq('b', iso(5))]
        self.write(cline(0, 4, 10, session='before'), cline(2, 5, 11), cline(6, 7, 14))
        snaps, got = qs.compute(recs, TABLE, claude=self.path)
        self.assertEqual({s['harness'] for s in snaps}, {'claude', 'codex'})
        self.assertEqual(qs.value(got[('claude', 'cs1', 'c1')]), 4.0)
        self.assertEqual(qs.value(got[('codex', 's2', 't2')]), 9.0)

    def test_a_reading_keeps_its_own_time_and_a_request_in_between_makes_the_step_shared(self):
        recs = [creq('a1', iso(1), turn='c1', session='A'), creq('b', iso(2), turn='c2', session='B', out=1000), creq('a2', iso(3), turn='c1', session='A')]
        self.write(cline(0, 10, 10, session='before'), cline(1, 15, 15, session='A'), cline(3, 20, 20, session='A'))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        self.assertEqual({s['t'] for s in snaps if s['who'][1] == 'A'}, {prompts._t(iso(1)), prompts._t(iso(3))})
        by = qs.turn_shares(recs, snaps, TABLE)
        share = by[('claude', 'A', 'c1')][10080]
        self.assertEqual(share['label'], 'estimate')  # B ran in between: not observed
        self.assertLess(qs.value(share), 10.0)

    def test_a_lone_turn_with_a_delayed_reading_is_still_observed(self):
        recs = [creq('a1', iso(1)), creq('a2', iso(3))]
        self.write(cline(0, 10, 10, session='before'), cline(1, 12, 12, seconds=20), cline(3, 15, 15, seconds=20))
        by = qs.turn_shares(recs, qs.snapshots_from_records(recs, claude=self.path), TABLE)
        share = by[('claude', 'cs1', 'c1')][10080]
        self.assertEqual((share['label'], qs.value(share), share['observed']['before'], share['observed']['after']), ('observed', 5.0, 10.0, 15.0))

    def test_sessionless_readings_do_not_break_counter_splitting(self):
        recs = [creq('a', iso(1), session='s1'), creq('b', iso(2), session='s2', turn='c2'), creq('c', iso(3), session='s1')]
        lines = []
        for m in range(0, 6):
            lines.append(json.dumps(dict(cline_row(m, 50 + 20 * (m % 2), 50 + 20 * (m % 2), session='s1' if m % 3 == 0 else 'x'), **({'session': None} if m % 2 else {}))))
        self.write(*lines)
        snaps, got = qs.compute(recs, TABLE, claude=self.path)  # must not raise
        self.assertTrue(snaps)
        from tokenatlas import insights, report
        report.build_report(recs, {}, now=datetime(2026, 9, 5, tzinfo=timezone.utc), claude_quota=self.path)
        qs.turn_shares(recs, snaps, TABLE)
        insights.cost_facts(recs, TABLE, quota=qs.turn_shares(recs, snaps, TABLE))

    def test_several_readings_after_one_request_count_its_cost_once(self):
        recs = [creq('a', iso(1))]
        self.write(cline(0, 1, 1, session='before'), cline(1, 2, 2, seconds=5), cline(1, 3, 3, seconds=10), cline(1, 4, 4, seconds=15))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        week = next(w for w in qs.windows(snaps, records=recs, cost_of=lambda r: 1.0) if w['minutes'] == 10080)
        self.assertEqual((week['cost'], week['unpriced_requests']), (1.0, 0))
        none = next(w for w in qs.windows(snaps, records=recs, cost_of=lambda r: None) if w['minutes'] == 10080)
        self.assertEqual((none['cost'], none['unpriced_requests']), (None, 1))

    def test_fractional_seconds_are_kept(self):
        recs = [creq('a', (T0 + timedelta(minutes=1, milliseconds=200)).isoformat())]
        row = dict(ts=(T0 + timedelta(minutes=1, milliseconds=700)).isoformat(), session='cs1', five_hour=None, seven_day=dict(used_percent=3, resets_at=RESET))
        self.write(json.dumps(row))
        got = qs.claude_snapshots(self.path, recs, prompts.assign_prompts(recs))
        self.assertEqual((got[0]['i'], got[0]['t'] - got[0]['at']), (0, timedelta(milliseconds=500)))

    def test_delayed_readings_conserve_the_movement_between_them(self):
        recs = [creq('a1', iso(1)), creq('a2', iso(1, 10))]  # requests at 60 s and 70 s; readings at 80 s and 90 s
        self.write(cline(0, 0, 0, session='before'), cline(1, 10, 10, seconds=20), cline(1, 20, 20, seconds=30))
        by = qs.turn_shares(recs, qs.snapshots_from_records(recs, claude=self.path), TABLE)
        share = by[('claude', 'cs1', 'c1')][10080]
        self.assertEqual(qs.value(share), 20.0)  # the whole movement, not the first 10 (an estimate: its first request has no reading of its own)

    def test_repeated_readings_after_one_request_give_the_turn_the_whole_movement(self):
        recs = [creq('a', iso(1))]
        self.write(cline(0, 0, 0, session='before'), cline(1, 5, 5, seconds=5), cline(1, 12, 12, seconds=10), cline(1, 20, 20, seconds=15))
        by = qs.turn_shares(recs, qs.snapshots_from_records(recs, claude=self.path), TABLE)
        share = by[('claude', 'cs1', 'c1')][10080]
        self.assertEqual((share['label'], qs.value(share), share['observed']['after']), ('observed', 20.0, 20.0))

    def test_staggered_delayed_readings_of_two_turns_add_up_to_the_movement(self):
        recs = [creq('a1', iso(1), turn='c1', session='A', out=1_000_000), creq('a2', iso(5), turn='c1', session='A', out=1_000_000),
                creq('b1', iso(3), turn='c2', session='B', out=1_000_000), creq('b2', iso(7), turn='c2', session='B', out=1_000_000)]
        self.write(cline(0, 0, 0, session='before'), cline(1, 4, 4, seconds=30, session='A'), cline(3, 9, 9, seconds=30, session='B'),
                   cline(5, 14, 14, seconds=30, session='A'), cline(7, 20, 20, seconds=30, session='B'))
        by = qs.turn_shares(recs, qs.snapshots_from_records(recs, claude=self.path), TABLE)
        mine = [by[k][10080] for k in (('claude', 'A', 'c1'), ('claude', 'B', 'c2'))]
        self.assertLessEqual(sum(x['lower'] for x in mine), 20.0)  # the bounds bracket the 20 points the two turns and the ones around them moved
        self.assertGreaterEqual(sum(x['upper'] for x in mine), 20.0)

    def test_a_reading_of_a_window_that_began_after_the_request_does_not_attach_to_it(self):
        recs = [creq('a', iso(0))]
        old5, new5 = (T0 + timedelta(hours=2)).isoformat(), (T0 + timedelta(hours=5, minutes=30)).isoformat()  # the new window began at T0 + 30 min
        self.write(cline(0, 90, 50, seconds=30, resets5=old5), cline(1, 5, 51, resets5=new5))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        five = [s for s in snaps if s['window'][0] == 300]
        self.assertEqual([(s['used_percent'], s['i']) for s in five], [(90, 0), (5, None)])
        self.assertEqual([s['i'] for s in snaps if s['window'][0] == 10080], [0, 0])  # the weekly window is the same one
        windows = qs.windows(snaps, records=recs, cost_of=lambda r: 1.0)
        self.assertEqual(sorted(w['cost'] or 0 for w in windows if w['minutes'] == 300), [0, 1.0])  # one cost allocation per window length
        self.assertEqual([w['cost'] for w in windows if w['minutes'] == 10080], [1.0])

    def test_change_only_recording_does_not_understate_the_window_cost(self):
        recs = [creq('a1', iso(1)), creq('a2', iso(3)), creq('a3', iso(5)), creq('a4', iso(30))]  # a2 and a4 have no reading: the value did not change
        self.write(cline(0, 4, 10, session='before'), cline(1, 5, 11, seconds=5), cline(5, 6, 12, seconds=5))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        for w in qs.windows(snaps, records=recs, cost_of=lambda r: 1.0):
            self.assertEqual((w['cost'], w['unpriced_requests']), (4.0, 0), w['minutes'])

    def test_an_expired_reading_followed_by_a_fresh_one_attaches_the_request_to_one_window(self):
        recs = [creq('a', iso(10))]
        expired5, fresh5 = (T0 + timedelta(minutes=5)).isoformat(), (T0 + timedelta(hours=5)).isoformat()  # the old window ended before the request
        self.write(cline(10, 90, 50, seconds=5, resets5=expired5), cline(10, 4, 50, seconds=10, resets5=fresh5))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        five = {s['used_percent']: s['i'] for s in snaps if s['window'][0] == 300}
        self.assertEqual(five, {90: None, 4: 0})
        windows = qs.windows(snaps, records=recs, cost_of=lambda r: 1.0)
        self.assertEqual(sum(w['cost'] or 0 for w in windows if w['minutes'] == 300), 1.0)  # not $2

    def test_a_request_exactly_at_a_reset_belongs_to_the_new_window_only(self):
        recs = [creq('a', iso(10))]
        old5, new5 = iso(10), (T0 + timedelta(hours=5, minutes=10)).isoformat()  # the old window ends at the request, the new one starts there
        self.write(cline(10, 90, 50, seconds=5, resets5=old5), cline(10, 4, 50, seconds=10, resets5=new5))
        snaps = qs.snapshots_from_records(recs, claude=self.path)
        self.assertEqual({s['used_percent']: s['i'] for s in snaps if s['window'][0] == 300}, {90: None, 4: 0})
        windows = qs.windows(snaps, records=recs, cost_of=lambda r: 1.0)
        self.assertEqual([w['cost'] or 0 for w in windows if w['minutes'] == 300].count(1.0), 1)  # one membership per window length
        self.assertEqual(sum(w['cost'] or 0 for w in windows if w['minutes'] == 300), 1.0)
        # the window's first instant is inside it
        self.write(cline(10, 4, 50, seconds=5, resets5=(T0 + timedelta(hours=5, minutes=10)).isoformat()))
        self.assertEqual([s['i'] for s in qs.snapshots_from_records(recs, claude=self.path) if s['window'][0] == 300], [0])


class ClaudeSurfaces(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'claude-quota.jsonl'
        self.path.write_text('\n'.join([cline(0, 4, 10, session='cs0'), cline(2, 5, 11), cline(6, 7, 14)]) + '\n')
        self.recs = [creq('0', iso(-5), turn='c0', session='cs0'), creq('a', iso(1)), creq('b', iso(5))]

    def test_report_payload_has_it_and_the_shared_report_no_session_ids(self):
        from tokenatlas import report
        payload = report.build_report(self.recs, {}, now=datetime(2026, 9, 5, tzinfo=timezone.utc), claude_quota=self.path)
        shares = [v for v in payload['quota_shares'].values() if v['harness'] == 'claude']
        self.assertEqual(sorted((v['minutes'], v['percent'], v['label']) for v in shares if v['percent']), [(10080, 4.0, 'observed')])
        self.assertEqual({w['harness'] for w in payload['quota_windows']}, {'claude'})
        text = json.dumps(payload)
        for secret in ('cs1', 'cs0', 'claude:cs1'):
            self.assertNotIn(secret, text)
        none = report.build_report(self.recs, {}, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertNotIn('quota_shares', none)

    def test_an_imported_harness_name_is_pseudonymized_in_a_shared_report(self):
        from tokenatlas import report
        recs = [dict(r, harness='evil-harness/secret') for r in history_records()]
        shared = report.build_report(recs, {}, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertNotIn('evil-harness', json.dumps(shared['quota_shares']) + json.dumps(shared['quota_windows']))  # (the context_size fact names harnesses itself, outside this change)
        private = report.build_report(recs, {}, redact=False, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual({v['harness'] for v in private['quota_shares'].values()}, {'evil-harness/secret'})
        known = report.build_report(history_records(), {}, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual({v['harness'] for v in known['quota_shares'].values()}, {'codex'})

    def test_top_and_insights_pick_it_up(self):
        from tokenatlas import insights, prompts
        top = prompts.top_prompts(self.recs, TABLE, 5)['prompts']
        qs.mark_turns(top, qs.compute(self.recs, TABLE, claude=self.path)[1])
        by = {p['turn_id']: p['quota_share'] for p in top}
        self.assertEqual((by['c1']['window_minutes'], by['c1']['delta_percent'], by['c1']['label']), (10080, 4.0, 'observed'))
        self.assertEqual(qs.line(by['c1'], 'claude'), '~4% of weekly Claude limit')
        got = qs.turn_shares(self.recs, qs.snapshots_from_records(self.recs, claude=self.path), TABLE)
        fact = next(f for f in insights.cost_facts(self.recs, TABLE, quota=got)['facts'] if f['id'] == 'quota_share')
        self.assertEqual([x['percent'] for x in fact['values']['turns']], [4.0])


class ClaudeCli(unittest.TestCase):
    """The commands read claude-quota.jsonl next to the history database."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / 'history.sqlite3'
        self.recs = [creq('0', iso(-5), turn='c0', session='cs0'), creq('a', iso(1)), creq('b', iso(5))]

    def call(self, *args):
        import contextlib, io
        from unittest import mock
        from tokenatlas import __main__ as cli
        from tokenatlas.history import History
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(History, 'records', lambda history, *a, **k: list(self.recs)), mock.patch.object(History, 'limit_events', lambda history: []), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--db', str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def prepare(self, quota=True):
        from tokenatlas.history import History
        with History(self.db):
            pass
        if quota:
            (self.dir / 'claude-quota.jsonl').write_text('\n'.join([cline(0, 4, 10, session='cs0'), cline(2, 5, 11), cline(6, 7, 14)]) + '\n')

    def test_top_json_shows_a_claude_share_only_with_the_file(self):
        self.prepare()
        code, out, _ = self.call('top', '--json')
        self.assertEqual(code, 0)
        by = {p['turn_id']: p['quota_share'] for p in json.loads(out)['prompts']}
        self.assertEqual((by['c1']['window_minutes'], by['c1']['delta_percent'], by['c1']['label']), (10080, 4.0, 'observed'))
        (self.dir / 'claude-quota.jsonl').unlink()
        _, out, _ = self.call('top', '--json')
        self.assertTrue(all(p['quota_share'] is None for p in json.loads(out)['prompts']))

    def test_doctor_reports_the_file_and_whether_recording_is_configured(self):
        from unittest import mock
        cfg = self.dir / 'claude'
        cfg.mkdir()
        self.prepare(quota=False)
        with mock.patch.dict('os.environ', {'CLAUDE_CONFIG_DIR': str(cfg)}):
            _, out, _ = self.call('doctor')
            off = json.loads(out)['claude_quota']
            self.assertEqual((off['snapshots_file'], off['snapshots'], off['last_snapshot'], off['recording_configured']), ('absent', 0, None, 'unknown'))
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': '/bin/tokenatlas statusline'}}))
            on = json.loads(self.call('doctor')[1])['claude_quota']
            self.assertEqual(on['recording_configured'], True)
            self.assertNotIn('warning', on)
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': '/bin/tokenatlas statusline --no-record-quota'}}))
            off = json.loads(self.call('doctor')[1])['claude_quota']
            self.assertEqual(off['recording_configured'], False)
            self.assertIn('disabled', off['warning'])
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': 'some-other-statusline'}}))
            foreign = json.loads(self.call('doctor')[1])['claude_quota']
            self.assertEqual(foreign['recording_configured'], False)
            self.assertIn("not tokenatlas's", foreign['warning'])
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': '/bin/tokenatlas statusline --record-quota'}}))
            self.assertEqual(json.loads(self.call('doctor')[1])['claude_quota']['recording_configured'], True)
            (cfg / 'settings.json').write_text('{ not json')
            self.assertEqual(json.loads(self.call('doctor')[1])['claude_quota']['recording_configured'], 'unknown')
            self.prepare()
            with open(self.dir / 'claude-quota.jsonl', 'a') as f:
                f.write('torn\n')
            on = json.loads(self.call('doctor')[1])['claude_quota']
        self.assertEqual((on['snapshots_file'], on['snapshots'], on['malformed'], on['last_snapshot']), ('present', 3, 1, prompts._t(iso(6)).isoformat()))
        self.assertGreater(on['last_snapshot_age'], 0)

    def test_the_whole_percent_note_does_not_claim_claude_reports_whole_percent(self):
        import json as j
        texts = j.loads((Path(__file__).parent / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        en, sv = texts['en']['ins_a_quota_whole'], texts['sv']['ins_a_quota_whole']
        self.assertIn('Shares are shown as whole percent', en)
        self.assertIn('Codex reports whole percent; Claude Code may report fractions', en)
        self.assertNotIn('counter moves in whole percent', en)
        self.assertIn('Codex rapporterar hela procent; Claude Code kan rapportera decimaler', sv)

    def test_report_and_open_use_the_file_and_a_change_rebuilds_a_cached_report(self):
        self.prepare()
        html = self.dir / 'r.html'
        code, _, _ = self.call('report', '--html', str(html), '--private', '--if-changed')
        self.assertEqual(code, 0)
        first = html.read_text()
        self.assertIn('claude', first)
        code, out, _ = self.call('report', '--html', str(html), '--private', '--if-changed')
        self.assertIn('unchanged', out)
        with open(self.dir / 'claude-quota.jsonl', 'a') as f:
            f.write(cline(7, 9, 16) + '\n')
        _, out, _ = self.call('report', '--html', str(html), '--private', '--if-changed')
        self.assertNotIn('unchanged', out)


if __name__ == '__main__':
    unittest.main()
