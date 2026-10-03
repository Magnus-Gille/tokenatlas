"""Limit hits (issue #91): rejected Claude requests and Codex reached-limit observations name the turn that hit a limit."""
import base64
import gzip
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import insights, limits, pricing, report, why
from tokenatlas.history import History, is_limit_event, _clean_quota

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 10, tzinfo=UTC)
START, END = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)
MODEL = 'claude-sonnet-5-5'
TABLE = pricing.load_prices()


def stamp(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def user(uuid, minutes, text='PRIVATE PROMPT TEXT', sidechain=False):
    return {'type': 'user', 'uuid': uuid, 'sessionId': 's1', 'cwd': '/work/app', 'timestamp': stamp(minutes),
            'isSidechain': sidechain, 'message': {'role': 'user', 'content': text}}


def call(request, minutes, out=1000, sidechain=False):
    return {'type': 'assistant', 'uuid': 'u-' + request, 'requestId': request, 'sessionId': 's1', 'cwd': '/work/app',
            'timestamp': stamp(minutes), 'isSidechain': sidechain,
            'message': {'id': 'm-' + request, 'model': MODEL, 'stop_reason': 'end_turn',
                        'usage': {'input_tokens': 10, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': out}}}


def rejected(request, minutes, kind='five_hour', resets=None, seconds=0, status='rejected', sidechain=False, **extra):
    resets = int((T0 + timedelta(hours=2)).timestamp()) if resets is None else resets
    quota = {'status': status, 'resetsAt': resets, 'overageStatus': 'rejected'}
    if kind is not None:
        quota['rateLimitType'] = kind
    zero = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    return {'type': 'assistant', 'uuid': 'u-' + request, 'requestId': request, 'sessionId': 's1', 'cwd': '/work/app',
            'timestamp': stamp(minutes, seconds), 'isSidechain': sidechain, 'error': 'rate_limit', 'isApiErrorMessage': True,
            'message': {'id': 'm-' + request, 'model': '<synthetic>', 'usage': zero, 'content': [{'type': 'text', 'text': 'limit'}]},
            'quotaLimits': quota, **extra}


def write(root, rows, name='s1.jsonl'):
    path = Path(root) / 'proj' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return path


def session_rows():
    """Two turns: a big one (a, b) then a small one (c) that hits the five-hour limit, with two retries."""
    return [user('t1', 0), call('a', 1, 50000), call('b', 2, 20000), user('t2', 30), call('c', 31, 1000),
            rejected('r1', 32), rejected('r2', 32, seconds=5), rejected('r3', 33)]


class ClaudeReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collect(self, rows, **kw):
        write(self.root, rows)
        return why.collect_claude(self.root, START, END, **kw)

    def test_rejected_row_is_kept_with_quota_zero_tokens_and_unknown_model(self):
        records = {r.call_id: r for r in self.collect(session_rows())}
        r = records['r1']
        self.assertEqual((r.fresh_input, r.cache_read, r.cache_write, r.output), (0, 0, 0, 0))
        self.assertEqual(r.model, 'unknown')
        self.assertEqual(r.quota, {'limit_id': None, 'plan_type': None, 'reached': 'five_hour', 'status': 'rejected',
                                   'resets_at': (T0 + timedelta(hours=2)).isoformat(), 'windows': [{'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0,
                                                'resets_at': (T0 + timedelta(hours=2)).isoformat()}]})
        self.assertIsNone(records['a'].quota)

    def test_retries_are_separate_records_attributed_to_the_current_turn(self):
        records = {r.call_id: r for r in self.collect(session_rows())}
        self.assertEqual({'r1', 'r2', 'r3'} <= set(records), True)
        self.assertEqual({records[k].turn_id for k in ('c', 'r1', 'r2', 'r3')}, {'t2'})

    def test_weekly_and_unknown_types(self):
        rows = [user('t1', 0), rejected('w', 1, 'seven_day'), rejected('x', 2, 'mystery_window', resets=None)]
        records = {r.call_id: r for r in self.collect(rows)}
        self.assertEqual(records['w'].quota['windows'][0]['minutes'], 10080)
        self.assertEqual(records['x'].quota['reached'], 'mystery_window')
        self.assertEqual(records['x'].quota['windows'], [])
        # an unknown type keeps its reset time (top level), so rejections are told apart by it
        resets = int((T0 + timedelta(hours=3)).timestamp())
        kept = {r.call_id: r for r in self.collect([user('t1', 0), rejected('y', 1, 'mystery_window', resets=resets)])}
        self.assertEqual(kept['y'].quota['resets_at'], (T0 + timedelta(hours=3)).isoformat())
        self.assertEqual(_clean_quota(kept['y'].quota)['resets_at'], (T0 + timedelta(hours=3)).isoformat())

    def test_rejection_in_a_subagent_file_is_kept(self):
        write(self.root, [user('t1', 0), call('a', 1)])
        path = self.root / 'proj' / 's1' / 'subagents' / 'agent-x1.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(rejected('sub', 5, sidechain=True, agentId='x1')) + '\n')
        records = {r.call_id: r for r in why.collect_claude(self.root, START, END)}
        self.assertEqual(records['sub'].thread_kind, 'subagent')
        self.assertEqual(records['sub'].quota['status'], 'rejected')

    def test_quota_limits_that_is_not_rejected_is_ignored(self):
        records = {r.call_id: r for r in self.collect([user('t1', 0), call('a', 1), rejected('ok', 2, status='allowed')])}
        self.assertNotIn('ok', records)


class HistoryLimitEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root / 'logs', session_rows())
        self.db = self.root / 'state' / 'h.sqlite3'

    def test_records_exclude_limit_events_and_limit_events_returns_them(self):
        with History(self.db) as h:
            h.refresh('claude', self.root / 'logs')
            self.assertEqual({r['id'] for r in h.records()}, {'a', 'b', 'c'})
            self.assertEqual({r['id'] for r in h.records(include_limit_events=True)}, {'a', 'b', 'c', 'r1', 'r2', 'r3'})
            events = h.limit_events()
            self.assertEqual({r['id'] for r in events}, {'r1', 'r2', 'r3'})
            self.assertTrue(all(is_limit_event(r) and r['model'] is None for r in events))
            self.assertEqual(len(h.limit_events(T0 + timedelta(minutes=33), None)), 1)
            self.assertEqual(h.doctor()['observations'], 6)

    def test_counts_and_costs_are_unchanged_by_limit_events(self):
        other = Path(self.tmp.name) / 'plain'
        write(other, [r for r in session_rows() if 'quotaLimits' not in r])
        with History(self.db) as h, History(Path(self.tmp.name) / 'plain.sqlite3') as p:
            h.refresh('claude', self.root / 'logs')
            p.refresh('claude', other)
            strip = lambda rows: [{k: v for k, v in r.items() if k not in ('machine', 'sources', 'quota')} for r in rows]
            self.assertEqual(strip(h.records()), strip(p.records()))
            self.assertEqual(sum(1 for _ in h.records()), 3)

    def test_rejection_without_a_valid_type_stays_a_limit_event(self):
        self.assertEqual(_clean_quota({'status': 'rejected', 'reached': None, 'windows': []}),
                         {'limit_id': None, 'plan_type': None, 'reached': None, 'windows': [], 'status': 'rejected', 'resets_at': None})
        self.assertIsNone(_clean_quota({'reached': None, 'windows': []}))
        write(self.root / 'untyped', [user('t1', 0), call('a', 1, 5000), rejected('r1', 2, kind=None, resets=0)])
        with History(self.root / 'untyped.sqlite3') as h:
            h.refresh('claude', self.root / 'untyped')
            self.assertEqual([r['id'] for r in h.records()], ['a'])
            events = h.limit_events()
            self.assertEqual([r['id'] for r in events], ['r1'])
            hits = limits.limit_hits(h.records(), events, TABLE)
            self.assertEqual((len(hits), hits[0]['window'], hits[0]['window_minutes']), (1, None, None))

    def test_snapshot_import_keeps_limit_events(self):
        snap = self.root / 'snap.sqlite3'
        with History(self.db) as h:
            h.refresh('claude', self.root / 'logs')
            h.snapshot(snap)
        with History(self.root / 'other.sqlite3') as o:
            o.import_snapshot(snap, 'laptop')
            self.assertEqual({r['id'] for r in o.limit_events()}, {'r1', 'r2', 'r3'})
            self.assertEqual({r['id'] for r in o.records()}, {'a', 'b', 'c'})

    def test_clean_quota_accepts_window_slots_and_rejected_status_only(self):
        q = {'reached': 'five_hour', 'status': 'rejected', 'windows': [
            {'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0, 'resets_at': '2026-09-03T12:00:00+00:00'},
            {'slot': 'bogus', 'minutes': 300, 'used_percent': 1.0, 'resets_at': None}]}
        cleaned = _clean_quota(q)
        self.assertEqual([w['slot'] for w in cleaned['windows']], ['five_hour'])
        self.assertEqual(cleaned['status'], 'rejected')
        self.assertNotIn('status', _clean_quota({**q, 'status': 'allowed'}))


def obs(id, ts, cost_tokens=0, harness='claude', provider='anthropic', session='s1', turn='t1', quota=None, **extra):
    return dict(id=id, ts=ts, harness=harness, provider=provider, machine='m', session=session, turn_id=turn, thread_kind='main',
                agent='main', model=MODEL if cost_tokens else None, quota=quota, complete=True, sources=[], raw_usage={}, tariff=None,
                tokens=dict(fresh_input=0, cache_read=0, cache_write=0, output=cost_tokens, reasoning=0),
                id_synthetic=False, warnings=[], parent_session=None, origin='cli', effort=None, project_id='/w/app', project_label='app',
                turn_confidence='observed', **extra)


def iso(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def five_hour(resets_min, status='rejected', reached='five_hour'):
    return {'limit_id': None, 'plan_type': None, 'reached': reached, 'status': status,
            'windows': [{'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0, 'resets_at': iso(resets_min)}]}


class LimitHits(unittest.TestCase):
    def test_no_hits(self):
        self.assertEqual(limits.limit_hits([obs('a', iso(0), 100)], [], TABLE), [])

    def test_claude_retries_are_one_hit_at_the_earliest_time(self):
        recs = [obs('a', iso(1), 100000)]
        events = [obs(f'r{i}', iso(30 + i), quota=five_hour(120)) for i in range(3)]
        hits = limits.limit_hits(recs, list(reversed(events)), TABLE)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h['harness'], h['at'], h['retries'], h['window_minutes'], h['reached']), ('claude', iso(30), 3, 300, 'five_hour'))
        self.assertEqual(h['turn'], ('claude', 's1', 't1'))
        # a different reset moment is another hit
        self.assertEqual(len(limits.limit_hits(recs, [*events, obs('z', iso(400), quota=five_hour(700))], TABLE)), 2)

    def test_window_ranks_turns_with_shares(self):
        recs = [obs('a', iso(-400), 900000, turn='old'),  # before the window: [120-300, 30] = [-180, 30]
                obs('b', iso(0), 100000, turn='big'), obs('c', iso(5), 20000, turn='mid'), obs('d', iso(6), 10000, turn='small'),
                obs('e', iso(7), 1000, turn='tiny'), obs('f', iso(8), 5, provider='openai', harness='codex', turn='x')]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='tiny', quota=five_hour(120))], TABLE)
        w = hits[0]['window']
        self.assertEqual([t['turn'][2] for t in w['top']], ['big', 'mid', 'small'])
        self.assertAlmostEqual(w['cost'], sum(t['cost'] for t in w['top']) + pricing_cost(recs[4]), places=9)
        self.assertAlmostEqual(sum(t['share'] for t in w['top']) + pricing_cost(recs[4]) / w['cost'], 1.0, places=9)
        self.assertEqual(w['requests'], 4)
        self.assertEqual(w['unpriced_requests'], 0)
        self.assertTrue(0 < w['top'][0]['share'] < 1)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 'tiny'))

    def test_unpriced_requests_are_counted_not_guessed(self):
        unknown = obs('u', iso(1), 5000, turn='odd')
        unknown['model'] = 'no-such-model'
        hits = limits.limit_hits([unknown, obs('b', iso(2), 1000, turn='b')], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['window']['unpriced_requests'], 1)
        self.assertEqual([t['turn'][2] for t in hits[0]['window']['top']], ['b'])

    def test_unknown_window_type_has_no_ranking(self):
        quota = {'limit_id': None, 'plan_type': None, 'reached': 'mystery', 'status': 'rejected', 'windows': []}
        hits = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=quota)], TABLE)
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0]['window'])
        self.assertIsNone(hits[0]['window_minutes'])

    def test_events_without_reset_time_group_only_within_a_gap(self):
        quota = {'limit_id': None, 'plan_type': None, 'reached': 'mystery', 'status': 'rejected', 'resets_at': None, 'windows': []}
        events = [obs('r1', iso(0), quota=quota), obs('r2', iso(5), quota=quota), obs('r3', iso(60 * 24 * 2), quota=quota)]
        hits = limits.limit_hits([], events, TABLE)
        self.assertEqual([(h['at'], h['retries']) for h in hits], [(iso(0), 2), (iso(60 * 24 * 2), 1)])
        withreset = dict(quota, resets_at=iso(500))
        hits = limits.limit_hits([], [obs('a', iso(0), quota=withreset), obs('b', iso(60 * 24 * 2), quota=withreset)], TABLE)
        self.assertEqual((len(hits), hits[0]['resets_at'], hits[0]['window']), (1, iso(500), None))

    def test_ambiguous_identity_is_excluded_and_incomplete_is_a_lower_bound(self):
        clean = obs('a', iso(1), 1000, turn='a')
        ambiguous = dict(obs('b', iso(2), 9000000, turn='b'), id_synthetic=True)
        hits = limits.limit_hits([clean, ambiguous], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        w = hits[0]['window']
        self.assertEqual((w['requests'], w['lower_bound']), (1, False))
        self.assertEqual([t['turn'][2] for t in w['top']], ['a'])
        partial = dict(obs('c', iso(3), 1000, turn='c'), complete=False)
        hits = limits.limit_hits([clean, partial], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertTrue(hits[0]['window']['lower_bound'])

    def test_codex_consecutive_state_is_per_harness_and_limit(self):
        def cx(id, minute, reached):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached, 'windows': []}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        recs = [cx('1', 1, 'workspace_owner_credits_depleted'), obs('mid', iso(2), 1000, turn='other'), cx('3', 3, 'workspace_owner_credits_depleted')]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def cx(self, id, minute, reached, windows, limit_id='codex'):
        quota = {'limit_id': limit_id, 'plan_type': 'plus', 'reached': reached, 'windows': windows}
        return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)

    @staticmethod
    def win(percent, resets, slot='primary', minutes=300):
        return {'slot': slot, 'minutes': minutes, 'used_percent': percent, 'resets_at': iso(resets)}

    def test_codex_episode_survives_drifting_reset_and_restarts_after_recovery(self):
        drift = [self.cx(str(i), i, 'rate_limit_reached', [self.win(100.0, 100 + i)]) for i in range(1, 5)]
        self.assertEqual([h['at'] for h in limits.limit_hits(drift, [], TABLE)], [iso(1)])
        again = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 2, None, [self.win(10.0, 100)]),
                 self.cx('3', 3, 'rate_limit_reached', [self.win(100.0, 700)])]  # a new window instance: its reset moved
        self.assertEqual([h['at'] for h in limits.limit_hits(again, [], TABLE)], [iso(1), iso(3)])
        same_instance = [again[0], again[1], self.cx('3', 3, 'rate_limit_reached', [self.win(100.0, 100)])]
        self.assertEqual(len(limits.limit_hits(same_instance, [], TABLE)), 1)  # flapping around 100 % within one reset time is one hit

    def test_codex_episode_expires_after_a_window_without_recovery(self):
        day = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 1 + 24 * 60, 'rate_limit_reached', [self.win(100.0, 100 + 24 * 60)])]
        self.assertEqual([h['at'] for h in limits.limit_hits(day, [], TABLE)], [iso(1), iso(1 + 24 * 60)])
        seconds = [self.cx(str(i), i, 'rate_limit_reached', [dict(self.win(100.0, 100), resets_at=(T0 + timedelta(minutes=100, seconds=7 * i)).isoformat())])
                   for i in range(1, 4)]
        self.assertEqual(len(limits.limit_hits(seconds, [], TABLE)), 1)

    def test_codex_reset_movement_alone_does_not_start_a_hit(self):
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 2, 'rate_limit_reached', [self.win(100.0, 130)])]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def test_codex_episode_ends_when_the_named_window_changes(self):
        weekly = self.win(100.0, 100, 'secondary', 10080)
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(20.0, 100, 'secondary', 10080)]),
                self.cx('2', 2, 'rate_limit_reached', [self.win(50.0, 100), weekly])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([h['window_minutes'] for h in hits], [300, 10080])

    def test_provider_aliases_count_in_the_window(self):
        def codex(id, minute, provider, turn):
            r = obs(id, iso(minute), 5000, harness='codex', provider=provider, session='c1', turn=turn)
            r['model'] = 'gpt-5.6-luna'
            return r
        hit = self.cx('hit', 100, 'rate_limit_reached', [self.win(100.0, 400)])
        alias_only = limits.limit_hits([codex('a', 10, 'openai-codex', 'ta'), hit], [], TABLE)[0]['window']
        self.assertEqual(alias_only['requests'], 1 + 1)  # the alias record and the hit's own observation (provider openai)
        mixed = limits.limit_hits([codex('a', 10, 'openai-codex', 'ta'), codex('b', 11, 'openai', 'tb'), hit], [], TABLE)[0]['window']
        self.assertEqual(mixed['requests'], 3)
        self.assertEqual({t['turn'][2] for t in mixed['top']} >= {'ta', 'tb'}, True)

    def test_unpriced_requests_make_window_and_turn_costs_lower_bounds(self):
        unknown = obs('u', iso(2), 5000, turn='mixed')
        unknown['model'] = 'no-such-model'
        recs = [obs('p', iso(1), 10000, turn='clean'), obs('p2', iso(3), 1000, turn='mixed'), unknown]
        w = limits.limit_hits(recs, [obs('r', iso(30), quota=five_hour(120))], TABLE)[0]['window']
        self.assertTrue(w['lower_bound'])
        self.assertEqual({t['turn'][2]: t['lower_bound'] for t in w['top']}, {'clean': False, 'mixed': True})
        payload = report.build_report(recs, {}, limit_hits=limits.limit_hits(recs, [obs('r', iso(30), quota=five_hour(120))], TABLE),
                                      now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual({t['lower_bound'] for t in payload['limit_hits'][0]['window']['top']}, {False, True})

    def test_subagent_rejection_takes_the_turn_of_its_thread(self):
        main = obs('m', iso(1), 1000, turn='t1')
        sub = dict(obs('s', iso(5), 90000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1')
        event = dict(obs('r', iso(6), turn=None, quota=five_hour(120)), thread_kind='subagent', agent='a1')
        hits = limits.limit_hits([main, sub], [event], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't1'))
        item = {'harness': 'claude', 'session': 's1', 'turn_id': 't1'}
        limits.mark_turns([item], hits)
        self.assertIn('limit_hit', item)
        early = dict(event, ts=iso(0))  # before any usage of the thread: the earliest after it
        self.assertEqual(limits.limit_hits([main, sub], [early], TABLE)[0]['turn'], ('claude', 's1', 't1'))

    def test_session_prefix_enforces_the_harness(self):
        recs = [obs('a', iso(1), 1000, turn='t1'), self.cx('c', 5, 'rate_limit_reached', [self.win(100.0, 100)])]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual(sorted(h['harness'] for h in hits), ['claude', 'codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, session='claude:s1')], ['claude'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, session='codex:c1')], ['codex'])
        self.assertEqual(limits.scope_hits(hits, recs, harness='codex', session='claude:s1'), [])

    def test_malformed_window_durations_are_dropped_without_crashing(self):
        huge = {'used_percent': 100, 'window_minutes': 10 ** 400, 'resets_at': 1790000000}
        quota = why._codex_quota({'limit_id': 'codex', 'primary': huge, 'secondary': dict(huge, window_minutes=527041),
                                  'rate_limit_reached_type': 'rate_limit_reached'})
        self.assertEqual(quota['windows'], [])
        edge = why._codex_quota({'primary': dict(huge, window_minutes=527040)})
        self.assertEqual(edge['windows'][0]['minutes'], 527040)
        cleaned = _clean_quota({'reached': 'x', 'windows': [{'slot': 'primary', 'minutes': 10 ** 400, 'used_percent': 100.0, 'resets_at': None},
                                                              {'slot': 'primary', 'minutes': 0, 'used_percent': 100.0, 'resets_at': None}]})
        self.assertEqual(cleaned['windows'], [])
        hits = limits.limit_hits([obs('a', iso(1), 1000, harness='codex', provider='openai', quota=quota)], [], TABLE)
        self.assertEqual((len(hits), hits[0]['window']), (1, None))

    def cxs(self, id, minute, session, reached, resets=100):
        r = self.cx(id, minute, reached, [self.win(100.0 if reached else 10.0, resets)])
        r['session'] = session
        return r

    def test_recovery_only_counts_from_a_session_that_reported_the_episode(self):
        stale = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'B', None), self.cxs('3', 3, 'A', 'rate_limit_reached')]
        self.assertEqual(len(limits.limit_hits(stale, [], TABLE)), 1)
        own = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'A', None), self.cxs('3', 3, 'A', 'rate_limit_reached', 700)]
        self.assertEqual(len(limits.limit_hits(own, [], TABLE)), 2)
        overlap = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'B', 'rate_limit_reached'), self.cxs('3', 3, 'B', None),
                   self.cxs('4', 4, 'A', 'rate_limit_reached', 700)]
        hits = limits.limit_hits(overlap, [], TABLE)
        self.assertEqual([h['at'] for h in hits], [iso(1), iso(4)])  # B's recovery ends the episode B joined, A's later report starts the next
        late = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 1 + 400, 'B', None), self.cxs('3', 2 + 400, 'A', 'rate_limit_reached', 900)]
        self.assertEqual(len(limits.limit_hits(late, [], TABLE)), 2)  # a gap longer than the window ends it

    def test_window_counts_only_the_hits_own_harness(self):
        claude = obs('c', iso(1), 5000, turn='tc')
        opencode = obs('o', iso(2), 90000, harness='opencode', provider='anthropic', session='oc', turn='to')  # API key usage, a different pool
        w = limits.limit_hits([claude, opencode], [obs('r', iso(30), quota=five_hour(120))], TABLE)[0]['window']
        self.assertEqual(w['requests'], 1)
        self.assertEqual([t['turn'][2] for t in w['top']], ['tc'])

    def test_scope_maps_rolled_up_subagents_through_the_full_assignment(self):
        main = obs('m', iso(1), 1000, turn='t1')
        sub = dict(obs('s', iso(5), 9000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1')
        sub['model'] = 'sub-model'
        recs = [main, sub]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't1'))
        self.assertEqual(len(limits.scope_hits(hits, [sub], agent='a1', universe=recs)), 1)
        self.assertEqual(len(limits.scope_hits(hits, [sub], model='sub-model', universe=recs)), 1)
        self.assertEqual(limits.scope_hits(hits, [], model='other', universe=recs), [])

    def test_codex_window_is_rolling_from_the_hit(self):
        # resets at +400 min: a reset-anchored window would start at +100 and miss the call at +20; the rolling window [hit-300, hit] has it
        recs = [obs('early', iso(20), 5000, harness='codex', provider='openai', session='c1', turn='early'),
                self.cx('hit', 250, 'rate_limit_reached', [self.win(100.0, 400)])]
        recs[0]['model'] = 'gpt-5.6-luna'
        h = limits.limit_hits(recs, [], TABLE)[0]
        self.assertEqual(h['window']['start'], iso(250 - 300))
        self.assertIn('early', [t['turn'][2] for t in h['window']['top']])

    def test_codex_window_selection(self):
        unknown = limits.limit_hits([self.cx('1', 1, 'something_new', [self.win(40.0, 100), self.win(20.0, 900, 'secondary', 10080)])], [], TABLE)[0]
        self.assertEqual((unknown['window_minutes'], unknown['window']), (None, None))
        both = limits.limit_hits([self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(100.0, 900, 'secondary', 10080)])], [], TABLE)
        self.assertEqual([(h['window_minutes'], h['reached']) for h in both], [(300, 'rate_limit_reached'), (10080, 'rate_limit_reached')])  # one hit per full window
        one = limits.limit_hits([self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(20.0, 900, 'secondary', 10080)])], [], TABLE)[0]
        self.assertEqual(one['window_minutes'], 300)

    def test_scope_hits(self):
        recs = [obs('a', iso(1), 1000, turn='t1'), self.cx('c', 5, 'rate_limit_reached', [self.win(100.0, 100)])]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual({h['harness'] for h in hits}, {'claude', 'codex'})
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, harness='codex')], ['codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, start=T0 + timedelta(minutes=10))], ['claude'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, end=T0 + timedelta(minutes=10))], ['codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, [recs[1]], session='c1')], ['codex'])
        self.assertEqual(len(limits.scope_hits(hits, recs)), 2)

    def test_codex_consecutive_reached_is_one_hit(self):
        def cx(id, minute, reached, used=100.0, resets=100):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached,
                     'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': used, 'resets_at': iso(resets)},
                                 {'slot': 'secondary', 'minutes': 10080, 'used_percent': 20.0, 'resets_at': iso(9000)}]}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        recs = [cx('1', 1, None, 50.0), cx('2', 2, 'rate_limit_reached'), cx('3', 3, 'rate_limit_reached'), cx('4', 4, None, 5.0, resets=400),
                cx('5', 5, 'rate_limit_reached', resets=400)]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['window_minutes'], h['harness']) for h in hits], [(iso(2), 300, 'codex'), (iso(5), 300, 'codex')])
        self.assertEqual(hits[0]['turn'], ('codex', 'c1', 'ct'))

    def test_codex_depleted_credits_is_a_hit_without_a_window(self):
        quota = {'limit_id': 'codex', 'plan_type': 'team', 'reached': 'workspace_owner_credits_depleted',
                 'windows': [{'slot': 'primary', 'minutes': 10080, 'used_percent': 40.0, 'resets_at': iso(9000)}]}
        hits = limits.limit_hits([obs('1', iso(1), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)], [], TABLE)
        self.assertEqual([(h['reached'], h['window_minutes'], h['window']) for h in hits], [('workspace_owner_credits_depleted', None, None)])
        self.assertEqual(limits.badge(hits[0]), 'Hit a limit (workspace_owner_credits_depleted)')

    def test_mark_turns_and_badge(self):
        hits = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        item = {'harness': 'claude', 'session': 's1', 'turn_id': 't1'}
        other = {'harness': 'claude', 'session': 's1', 'turn_id': 'zz'}
        limits.mark_turns([item, other], hits)
        self.assertEqual(item['limit_hit'], {'reached': 'five_hour', 'window_minutes': 300, 'at': iso(30)})
        self.assertNotIn('limit_hit', other)
        self.assertEqual(limits.badge(hits[0]), 'Hit the 5-hour limit')


def pricing_cost(record):
    from tokenatlas import prompts
    return prompts._cost(record, TABLE)


class Surfaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        write(root / 'logs', session_rows())
        with History(root / 'h.sqlite3') as h:
            h.refresh('claude', root / 'logs')
            self.records = h.records()
            self.events = h.limit_events()
            self.status = h.doctor()
        self.hits = limits.limit_hits(self.records, self.events, TABLE)

    def build(self, **kw):
        return report.build_report(self.records, self.status, limit_hits=self.hits, now=datetime(2026, 9, 5, tzinfo=UTC), **kw)

    def test_report_payload_and_page(self):
        payload = self.build()
        self.assertEqual(len(payload['limit_hits']), 1)
        h = payload['limit_hits'][0]
        self.assertEqual((h['window_minutes'], h['retries']), (300, 3))
        self.assertIsInstance(h['prompt'], int)
        self.assertEqual(h['window']['top'][0]['prompt'], 0)
        page = report.render_report(payload)
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        for lang, words in (('sv', ('Gränsträffar', 'Slog i 5-timmarsgränsen', 'andel av det tokenatlas såg')),
                            ('en', ('Limit hits', 'Hit the 5-hour limit', 'share of what tokenatlas saw'))):
            blob = json.dumps(i18n[lang], ensure_ascii=False)
            for w in words:
                self.assertIn(w, blob)
            self.assertNotIn('primary', i18n[lang]['lh_head'])
        self.assertIn('claude.ai', i18n['en']['lh_cover'])
        self.assertIn('claude.ai', i18n['sv']['lh_cover'])
        self.assertIn('id="limit-hits" class="panel section hidden"', page)

    def test_no_hits_no_section_data(self):
        payload = report.build_report(self.records, self.status, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertNotIn('limit_hits', payload)
        self.assertFalse(any(f['id'] == 'limit_hits' for w in payload['insights']['windows'] for f in w['facts']))

    def test_shared_report_has_no_session_ids_or_prompt_text(self):
        payload = self.build(redact=True)
        blob = json.dumps(payload['limit_hits'])
        for secret in ('s1', 'PRIVATE', '/work/app', 't1', 't2'):
            self.assertNotIn(f'"{secret}"', blob)
        self.assertNotIn('PRIVATE', json.dumps(payload))
        self.assertEqual(set(payload['limit_hits'][0]), {'harness', 'at', 'local_date', 'reached', 'window_minutes', 'resets_at', 'retries', 'prompt', 'label', 'window'})

    def test_private_report_gives_each_hit_its_filter_scope_and_the_shared_report_none(self):
        shared, private = self.build(redact=True), self.build(redact=False)
        self.assertNotIn('scope', shared['limit_hits'][0])
        scope = private['limit_hits'][0]['scope']
        self.assertEqual(set(scope), {'project_id', 'session', 'provider', 'model', 'effort', 'agent'})
        self.assertTrue(scope['session'].startswith('claude:'))

    def test_insights_fact(self):
        result = insights.cost_facts(self.records, TABLE, hits=self.hits)
        fact = next(f for f in result['facts'] if f['id'] == 'limit_hits')
        self.assertEqual(fact['values']['count'], 1)
        self.assertEqual(fact['values']['limits'], [{'limit': 'five_hour', 'harness': 'claude', 'count': 1}])
        self.assertIn('5-hour limit', insights.render_text(result))
        self.assertFalse(any(f['id'] == 'limit_hits' for f in insights.cost_facts(self.records, TABLE)['facts']))
        late = insights.cost_facts(self.records, TABLE, start=datetime(2026, 9, 4, tzinfo=UTC), hits=self.hits)
        self.assertFalse(any(f['id'] == 'limit_hits' for f in late['facts']))


class Privacy(unittest.TestCase):
    def setUp(self):
        self.recs = [obs('a', iso(1), 100000, turn='t1')]
        self.quota = {'limit_id': None, 'plan_type': None, 'reached': 'private-session-xyz', 'status': 'rejected', 'resets_at': iso(120), 'windows': []}
        self.events = [obs('r', iso(30), turn='t2', quota=self.quota)]
        self.hits = limits.limit_hits(self.recs, self.events, TABLE)
        self.now = datetime(2026, 9, 5, tzinfo=UTC)

    def test_unknown_limit_type_is_neutral_in_shared_reports_and_insights(self):
        shared = report.build_report(self.recs, {}, redact=True, limit_hits=self.hits, now=self.now)
        self.assertNotIn('private-session-xyz', json.dumps(shared))
        self.assertEqual(shared['limit_hits'][0]['reached'], 'other')
        self.assertNotIn('private-session-xyz', json.dumps(insights.cost_facts(self.recs, TABLE, hits=self.hits)))
        private = report.build_report(self.recs, {}, redact=False, limit_hits=self.hits, now=self.now)
        self.assertEqual(private['limit_hits'][0]['reached'], 'private-session-xyz')
        known = limits.limit_hits(self.recs, [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertEqual(report.build_report(self.recs, {}, redact=True, limit_hits=known, now=self.now)['limit_hits'][0]['reached'], 'five_hour')

    def test_rejection_only_turn_gets_a_safe_label(self):
        for redact in (True, False):
            payload = report.build_report(self.recs, {}, redact=redact, limit_hits=self.hits, now=self.now,
                                          **({} if redact else {'prompt_texts': {('claude', 's1', 't2'): 'Refactor it'}}))
            h = payload['limit_hits'][0]
            self.assertIsNone(h['prompt'])  # no successful call: no card
            self.assertEqual((h['label']['at'], h['label']['harness']), (iso(30), 'claude'))
            self.assertEqual(h['label']['text'], None if redact else 'Refactor it')
            self.assertNotIn('t2', json.dumps(h))

    def test_turn_outside_the_card_list_keeps_a_label(self):
        recs = [obs(f'o{i}', iso(i), 1000 * (i + 1), turn=f'turn{i}') for i in range(12)]
        hits = limits.limit_hits(recs, [obs('r', iso(40), turn='turn11', quota=five_hour(120))], TABLE)
        top = report.build_report(recs, {}, redact=True, limit_hits=hits, now=self.now)['limit_hits'][0]['window']['top']
        self.assertTrue(all(t['label'] and t['label']['at'] and 'text' in t['label'] for t in top))
        self.assertTrue(all(t['prompt'] is not None for t in top))


class SingleAssignment(unittest.TestCase):
    def test_rejection_only_parent_turn_and_subagent_usage_agree_with_the_cards(self):
        sub = dict(obs('sub', iso(40), 90000, turn=None), thread_kind='subagent', parent_session='s1', session='s1', agent='a1')
        recs = [obs('a', iso(1), 1000, turn='t1'), sub]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t2', quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't2'))  # a rejection carries its own turn
        top = hits[0]['window']['top'][0]
        payload = report.build_report(recs, {}, redact=True, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        columns = payload['columns']
        ordinal = columns['prompt'][[i for i, x in enumerate(columns['id']) if x == 2][0]]  # the subagent row (second observation)
        self.assertEqual(payload['limit_hits'][0]['window']['top'][0]['prompt'], ordinal)
        self.assertEqual(top['turn'][2], 't1')  # usage-only assignment: the same as top_prompts and the cards
        self.assertIsNone(payload['limit_hits'][0]['prompt'])  # the rejection-only turn has no card


class RoundSeven(unittest.TestCase):
    def test_subagent_whose_first_request_is_rejected_gets_the_parent_turn(self):
        main = obs('m', iso(1), 1000, turn='t1')
        event = dict(obs('r', iso(6), turn=None, quota=five_hour(120)), thread_kind='subagent', agent='a9', parent_session='s1')
        hits = limits.limit_hits([main], [event], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't1'))
        payload = report.build_report([main], {}, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual(payload['limit_hits'][0]['prompt'], 0)  # the badge's card: the parent turn
        item = {'harness': 'claude', 'session': 's1', 'turn_id': 't1'}
        limits.mark_turns([item], hits)
        self.assertIn('limit_hit', item)

    def test_window_without_bounds_is_told_apart_from_an_unknown_duration(self):
        page = (Path(report.__file__).with_name('report_template.html')).read_text(encoding='utf-8')
        self.assertIn("t(h.window_minutes?'lh_nobounds':'lh_nowindow')", page)
        i18n = json.loads(Path(report.__file__).with_name('report_i18n.json').read_text(encoding='utf-8'))
        self.assertIn('start is unknown', i18n['en']['lh_nobounds'])
        self.assertIn('start är okänd', i18n['sv']['lh_nobounds'])
        no_reset = dict(five_hour(120), resets_at=None)
        no_reset['windows'] = [dict(no_reset['windows'][0], resets_at=None)]
        hit = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=no_reset)], TABLE)[0]
        self.assertEqual((hit['window_minutes'], hit['window']), (300, None))  # known duration, no bounds -> "start is unknown"
        unknown = dict(five_hour(120), reached='mystery', windows=[])
        hit = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=unknown)], TABLE)[0]
        self.assertEqual((hit['window_minutes'], hit['window']), (None, None))  # unknown duration

    def test_filtered_cards_and_facts_agree_on_a_cut_subagent_thread(self):
        main = dict(obs('m', iso(1), 400000, turn='t1'))
        subs = [dict(obs(f's{i}', iso(10 + i), 400000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1') for i in range(2)]
        later = obs('l', iso(30), 1000, turn='t2')
        universe = [main, later, *subs]
        selected = [later, subs[1]]  # a filter that cuts the thread: its first request is out, the rest still belongs to t1
        payload = report.build_report(selected, {}, redact=True, universe=universe, now=datetime(2026, 9, 5, tzinfo=UTC))
        cards = set(payload['columns']['prompt'])
        self.assertEqual(len(cards), 2)
        facts = insights.cost_facts(selected, TABLE, big_turn=1.0, universe=universe)
        big = next(f for f in facts['facts'] if f['id'] == 'big_turns')['values']
        self.assertEqual(big['turns'], 2)  # t1 (the subagent request) and t2
        self.assertNotIn('big_turns', {f['id'] for f in insights.cost_facts(selected, TABLE, big_turn=1.0)['facts']})  # without the universe the subagent has no turn


class RoundEight(unittest.TestCase):
    def test_unrecognized_harness_is_neutral_in_the_cost_facts_of_a_shared_report(self):
        event = dict(obs('r', iso(30), quota=five_hour(120)), harness='private-harness-xyz')
        hits = limits.limit_hits([], [event], TABLE)
        self.assertEqual(hits[0]['harness'], 'private-harness-xyz')
        facts = insights.cost_facts([obs('a', iso(1), 1000)], TABLE, hits=hits)
        fact = next(f for f in facts['facts'] if f['id'] == 'limit_hits')
        self.assertEqual(fact['values']['limits'], [{'limit': 'five_hour', 'harness': 'other', 'count': 1}])
        self.assertNotIn('private-harness-xyz', json.dumps(facts) + insights.render_text(facts))
        shared = report.build_report([obs('a', iso(1), 1000)], {}, redact=True, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertNotIn('private-harness-xyz', json.dumps(shared))

    def test_stored_oversized_window_duration_never_crashes(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        from tokenatlas.history import _encode, normalize
        quota = {'limit_id': 'codex', 'plan_type': 'pro', 'reached': 'rate_limit_reached', 'windows': [
            {'slot': 'primary', 'minutes': 10 ** 400, 'used_percent': 100.0, 'resets_at': iso(500)},
            {'slot': 'secondary', 'minutes': 'x', 'used_percent': 100.0, 'resets_at': None}]}
        record = why.AttributionRecord(harness='codex', provider='openai', timestamp=T0, session_id='c1', call_id='bad', model='gpt-5.6-luna',
                                       effort=None, project='app', entrypoint='cli', thread_kind='main', agent='main',
                                       fresh_input=10, cache_read=0, cache_write=0, output=5, reasoning=0,
                                       raw_usage={'input_tokens': 10, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0, 'output_tokens': 5})
        item = normalize(record, 'm')
        item['quota'] = quota  # bypass normalize's validation, as an older database or an import would
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'h.sqlite3'
            with History(db) as h:
                h._insert(h.connection, _encode(item))
                h.connection.commit()
                hits = limits.limit_hits(h.records(), [], TABLE)
            self.assertEqual((len(hits), hits[0]['window_minutes'], hits[0]['window']), (1, None, None))
            def run(*args):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return main(['--db', str(db), *args])
            self.assertEqual(run('top', '--json'), 0)
            self.assertEqual(run('report', '--html', str(Path(tmp) / 'r.html')), 0)
            self.assertEqual(run('insights', '--json'), 0)

    def test_omitted_type_is_neutral_unless_the_window_is_below_full(self):
        def cx(id, minute, reached, percent, resets=100):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached,
                     'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': percent, 'resets_at': iso(resets)}]}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        still_full = [cx('1', 1, 'rate_limit_reached', 100.0), cx('2', 2, None, 100.0), cx('3', 3, 'rate_limit_reached', 100.0)]
        self.assertEqual(len(limits.limit_hits(still_full, [], TABLE)), 1)
        recovered = [cx('1', 1, 'rate_limit_reached', 100.0), cx('2', 2, None, 40.0), cx('3', 3, 'rate_limit_reached', 100.0, 700)]
        self.assertEqual(len(limits.limit_hits(recovered, [], TABLE)), 2)

    def test_synthetic_parent_still_anchors_the_assignment_of_a_subagent(self):
        parent = dict(obs('m', iso(1), 400000, turn='t1'), id_synthetic=True)
        subs = [dict(obs(f's{i}', iso(10 + i), 400000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1') for i in range(2)]
        later = obs('l', iso(30), 1000, turn='t2')
        universe = [parent, later, *subs]
        facts = insights.cost_facts([later, subs[1]], TABLE, big_turn=1.0, universe=universe)
        big = next(f for f in facts['facts'] if f['id'] == 'big_turns')['values']
        self.assertEqual(big['turns'], 2)  # t1 is anchored by the ambiguous parent, whose own tokens are not counted
        payload = report.build_report([later, subs[1]], {}, redact=True, universe=universe, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual(len(set(payload['columns']['prompt'])), 2)


class RoundNine(unittest.TestCase):
    @staticmethod
    def cx(id, minute, reached, windows, limit_id='codex'):
        quota = {'limit_id': limit_id, 'plan_type': 'plus', 'reached': reached, 'windows': windows}
        return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)

    @staticmethod
    def win(percent, minutes, slot):
        return {'slot': slot, 'minutes': minutes, 'used_percent': percent, 'resets_at': iso(100)}

    def test_imported_reset_text_never_reaches_a_shared_report(self):
        secret = 'PRIVATE-PROMPT-OR-SESSION-ID'
        codex = self.cx('c', 5, 'rate_limit_reached', [dict(self.win(100.0, 300, 'primary'), resets_at=secret)])
        claude_quota = dict(five_hour(120), resets_at=secret)
        claude_quota['windows'] = [dict(claude_quota['windows'][0], resets_at=secret)]
        event = obs('r', iso(30), quota=claude_quota)
        top_level = obs('r2', iso(400), quota=dict(claude_quota, windows=[], reached='mystery'))
        hits = limits.limit_hits([codex], [event, top_level], TABLE)
        self.assertEqual(len(hits), 3)
        self.assertTrue(all(h['resets_at'] is None for h in hits))
        shared = report.build_report([codex], {}, redact=True, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertNotIn(secret, json.dumps(shared))
        naive = self.cx('n', 5, 'rate_limit_reached', [dict(self.win(100.0, 300, 'primary'), resets_at='2026-09-03T12:00:00')])
        self.assertIsNone(limits.limit_hits([naive], [], TABLE)[0]['resets_at'])
        good = self.cx('g', 5, 'rate_limit_reached', [dict(self.win(100.0, 300, 'primary'), resets_at='2026-09-03T14:00:00Z')])
        self.assertEqual(limits.limit_hits([good], [], TABLE)[0]['resets_at'], '2026-09-03T14:00:00+00:00')

    def test_both_windows_full_stay_one_episode_until_both_are_below_full(self):
        both = lambda a, b: [self.win(a, 300, 'primary'), self.win(b, 10080, 'secondary')]
        recs = [self.cx('1', 1, 'rate_limit_reached', both(100.0, 100.0)), self.cx('2', 2, None, both(100.0, 100.0)),
                self.cx('3', 3, 'rate_limit_reached', both(100.0, 100.0))]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['window_minutes'], h['reached']) for h in hits], [(300, 'rate_limit_reached'), (10080, 'rate_limit_reached')])  # one per window
        self.assertEqual(insights.cost_facts(recs, TABLE, hits=hits)['facts'][-1]['values']['count'], 2)
        # the weekly window recovers, the 5-hour stays full; re-reaching the same weekly instance (same reset) is the same hit
        half = [recs[0], self.cx('2', 2, None, both(100.0, 10.0)), recs[2]]
        self.assertEqual(len(limits.limit_hits(half, [], TABLE)), 2)
        # both recover and both fill again in a new window instance: two more hits
        again = [dict(self.win(100.0, 300, 'primary'), resets_at=iso(700)), dict(self.win(100.0, 10080, 'secondary'), resets_at=iso(20000))]
        recovered = [recs[0], self.cx('2', 2, None, both(10.0, 10.0)), self.cx('3', 3, 'rate_limit_reached', again)]
        self.assertEqual(len(limits.limit_hits(recovered, [], TABLE)), 4)

    def test_credit_exhaustion_has_no_duration_expiry(self):
        recs = [self.cx('1', 1, 'workspace_owner_credits_depleted', []), self.cx('2', 1 + 301, 'workspace_owner_credits_depleted', [])]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)
        recovered = [recs[0], self.cx('r', 100, None, []), recs[1]]
        self.assertEqual(len(limits.limit_hits(recovered, [], TABLE)), 2)


class RoundTen(unittest.TestCase):
    def test_collector_keeps_the_recovery_snapshot_so_exhaustion_recovery_exhaustion_is_two_hits(self):
        from test_why_codex import _limits, _meta, _quota_call, _write_rollout
        depleted = _limits(rate_limit_reached_type='workspace_owner_credits_depleted')
        rows = [_meta('c1'), _quota_call(stamp(1), 1, depleted), _quota_call(stamp(2), 2, _limits()), _quota_call(stamp(3), 3, depleted)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / 'codex' / 'rollout-c.jsonl', rows)
            with History(root / 'h.sqlite3') as h:
                h.refresh('codex', root / 'codex')
                records = h.records()
                self.assertEqual([r['quota']['reached'] for r in records], ['workspace_owner_credits_depleted', None, 'workspace_owner_credits_depleted'])
                self.assertEqual(records[1]['quota']['windows'], [])
                self.assertEqual(len(limits.limit_hits(records, [], TABLE)), 2)

    def test_clean_quota_keeps_an_empty_snapshot_with_a_plan_or_limit(self):
        self.assertEqual(_clean_quota({'limit_id': 'codex', 'plan_type': None, 'reached': None, 'windows': []})['windows'], [])
        self.assertIsNone(_clean_quota({'limit_id': None, 'plan_type': None, 'reached': None, 'windows': []}))

    @staticmethod
    def cx(id, minute, reached, windows):
        quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached, 'windows': windows}
        return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)

    def test_episode_started_without_a_window_learns_it_later(self):
        full = [{'slot': 'primary', 'minutes': 300, 'used_percent': 100.0, 'resets_at': iso(100)}]
        recs = [self.cx('1', 1, 'rate_limit_reached', []), self.cx('2', 2, 'rate_limit_reached', full), self.cx('3', 3, None, full),
                self.cx('4', 4, 'rate_limit_reached', full)]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual(len(hits), 1)
        # the later full window uniquely identifies the window: the existing hit learns its duration and reset (its time stays) and gets a ranking
        self.assertEqual((hits[0]['at'], hits[0]['window_minutes'], hits[0]['resets_at']), (iso(1), 300, iso(100)))
        earlier = obs('e', iso(-30), 5000, harness='codex', provider='openai', session='c1', turn='early')
        earlier['model'] = 'gpt-5.6-luna'
        ranked = limits.limit_hits([earlier, *recs], [], TABLE)[0]['window']
        self.assertEqual([t['turn'][2] for t in ranked['top']], ['early'])
        # a provisional (unknown-window) episode ends when a participating session reports no type
        none = [self.cx('1', 1, 'rate_limit_reached', []), self.cx('2', 2, None, []), self.cx('3', 3, 'rate_limit_reached', [])]
        self.assertEqual(len(limits.limit_hits(none, [], TABLE)), 2)

    def test_claude_retries_with_and_without_a_reset_are_one_hit(self):
        with_reset, without = five_hour(120), dict(five_hour(120), windows=[], resets_at=None)
        for order in ((with_reset, without, with_reset), (without, with_reset, with_reset), (without, without, with_reset)):
            events = [obs(f'r{i}', iso(30 + i), quota=q) for i, q in enumerate(order)]
            hits = limits.limit_hits([], events, TABLE)
            self.assertEqual((len(hits), hits[0]['retries'], hits[0]['resets_at']), (1, 3, iso(120)), order)
        far = [obs('a', iso(30), quota=with_reset), obs('b', iso(30 + 60 * 24 * 2), quota=without)]
        self.assertEqual(len(limits.limit_hits([], far, TABLE)), 2)


class RoundEleven(unittest.TestCase):
    def history(self, rows):
        from test_why_codex import _meta, _write_rollout
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _write_rollout(root / 'codex' / 'rollout-c.jsonl', [_meta('c1'), *rows])
        h = History(root / 'h.sqlite3').__enter__()
        self.addCleanup(h.__exit__, None, None, None)
        h.refresh('codex', root / 'codex')
        return h

    @staticmethod
    def snap(percent, reached=None, hours=3):
        from test_why_codex import _limits, _window
        return _limits(primary=_window(percent, 300, int((T0 + timedelta(hours=hours)).timestamp())), **({'rate_limit_reached_type': reached} if reached else {}))

    @staticmethod
    def calls(*specs):
        """Token-count rows with the same cumulative counters (unchanged) and distinct ordinals, one per (minute, snapshot)."""
        from test_why_codex import _quota_call
        rows = []
        for i, (minute, snapshot) in enumerate(specs):
            row = _quota_call(stamp(minute), 1, snapshot)
            row['ordinal'] = 100 + i
            rows.append(row)
        return rows

    def hits(self, h):
        return limits.limit_hits(h.records(), h.limit_events(), TABLE)

    def test_unchanged_counters_with_a_reached_snapshot_are_one_hit(self):
        from test_why_codex import _quota_call
        h = self.history([_quota_call(stamp(1), 1, self.snap(60.0)), _quota_call(stamp(2), 1, self.snap(100.0, 'rate_limit_reached'))])
        self.assertEqual(len(h.records()), 1)
        events = h.limit_events()
        self.assertEqual([(e['quota']['status'], e['model']) for e in events], [('event', None)])
        self.assertEqual([x['tokens']['output'] for x in events], [0])
        hits = self.hits(h)
        self.assertEqual((len(hits), hits[0]['harness'], hits[0]['window_minutes']), (1, 'codex', 300))

    def test_quota_only_event_with_no_info_is_one_hit(self):
        from test_why_codex import _quota_call
        quota_only = {'timestamp': stamp(2), 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None,
                                                                              'rate_limits': self.snap(100.0, 'rate_limit_reached')}}
        h = self.history([_quota_call(stamp(1), 1, self.snap(60.0)), quota_only])
        self.assertEqual(len(self.hits(h)), 1)
        self.assertEqual({r['id'] for r in h.records()}, {r['id'] for r in h.records() if not r['quota'] or r['quota'].get('status') != 'event'})

    def test_recovery_with_unchanged_counters_between_two_exhaustions_is_two_hits(self):
        from test_why_codex import _quota_call
        h = self.history(self.calls((1, self.snap(60.0)), (2, self.snap(100.0, 'rate_limit_reached')), (3, self.snap(10.0)),
                                    (4, self.snap(100.0, 'rate_limit_reached', hours=9))))
        self.assertEqual(len(h.limit_events()), 3)
        self.assertEqual(len(self.hits(h)), 2)

    def test_no_transition_adds_no_records(self):
        from test_why_codex import _quota_call
        h = self.history(self.calls((1, self.snap(60.0)), (2, self.snap(61.0)), (3, self.snap(62.0)), (4, self.snap(100.0, 'rate_limit_reached')),
                                    (5, self.snap(100.0, 'rate_limit_reached'))))
        self.assertEqual(len(h.limit_events()), 1)  # only the transition to reached; the repeats and the 60 -> 62 drift add nothing
        self.assertEqual(len(h.records()), 1)

    def test_statusline_cache_ignores_codex_quota_events(self):
        from tokenatlas import statusline
        from test_why_codex import _quota_call
        h = self.history([_quota_call(stamp(1), 1, self.snap(60.0)), _quota_call(stamp(2), 1, self.snap(100.0, 'rate_limit_reached'))])
        cache = statusline.build_cache(h, T0 + timedelta(hours=1))
        self.assertEqual(sum(d['requests'] for d in cache['days'].values()), 1)

    def test_resetless_claude_retry_after_the_known_reset_is_a_new_hit(self):
        without = dict(five_hour(0), windows=[], resets_at=None)
        events = lambda minute: [obs('a', iso(4), quota=five_hour(5)), obs('b', iso(minute), quota=without)]
        self.assertEqual(len(limits.limit_hits([], events(4.5), TABLE)), 1)
        self.assertEqual(len(limits.limit_hits([], events(6), TABLE)), 2)


class WindowFull(unittest.TestCase):
    @staticmethod
    def cx(id, minute, percent, session='c1', reached=None, minutes=300, resets=500):
        quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached,
                 'windows': [{'slot': 'primary', 'minutes': minutes, 'used_percent': percent, 'resets_at': iso(resets)}]}
        r = obs(id, iso(minute), 1000, harness='codex', provider='openai', session=session, turn='ct', quota=quota)
        r['model'] = 'gpt-5.6-luna'
        return r

    def test_a_window_reaching_full_is_a_hit_and_lasts_while_it_stays_full(self):
        recs = [self.cx('1', 1, 99.0), self.cx('2', 2, 100.0), self.cx('3', 3, 100.0), self.cx('4', 4, 40.0, resets=800), self.cx('5', 5, 100.0, resets=800)]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['reached'], h['window_minutes']) for h in hits], [(iso(2), 'window_full', 300), (iso(5), 'window_full', 300)])
        self.assertEqual(hits[0]['turn'], ('codex', 'c1', 'ct'))
        self.assertEqual(hits[0]['window']['start'], iso(2 - 300))  # the trailing window, own harness only
        self.assertEqual(limits.badge(hits[0]), 'Hit the 5-hour limit')
        weekly = limits.limit_hits([self.cx('1', 1, 99.0, minutes=10080), self.cx('2', 2, 100.0, minutes=10080)], [], TABLE)
        self.assertEqual((weekly[0]['window_minutes'], limits.badge(weekly[0])), (10080, 'Hit the weekly limit'))

    def test_a_stale_lower_reading_from_another_session_does_not_end_it(self):
        recs = [self.cx('1', 1, 100.0, 'A'), self.cx('2', 2, 98.0, 'B'), self.cx('3', 3, 100.0, 'A')]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)
        own = [self.cx('1', 1, 100.0, 'A'), self.cx('2', 2, 98.0, 'A', resets=800), self.cx('3', 3, 100.0, 'A', resets=800)]
        self.assertEqual(len(limits.limit_hits(own, [], TABLE)), 2)
        late = [self.cx('1', 1, 100.0, 'A'), self.cx('2', 1 + 301, 100.0, 'A', resets=900)]
        self.assertEqual(len(limits.limit_hits(late, [], TABLE)), 2)  # a gap longer than the window ends it

    def test_flapping_readings_of_one_window_instance_are_one_hit(self):
        recs = [self.cx('1', 1, 100.0), self.cx('2', 2, 97.0), self.cx('3', 3, 100.0), self.cx('4', 4, 96.0), self.cx('5', 5, 100.0)]  # one reset time
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def test_a_reached_type_and_a_full_window_together_are_one_hit(self):
        together = [self.cx('1', 1, 100.0, reached='rate_limit_reached')]
        hits = limits.limit_hits(together, [], TABLE)
        self.assertEqual([(h['reached'], h['window_minutes']) for h in hits], [('rate_limit_reached', 300)])
        window_first = [self.cx('1', 1, 100.0), self.cx('2', 2, 100.0, reached='rate_limit_reached')]
        hits = limits.limit_hits(window_first, [], TABLE)
        self.assertEqual([(h['at'], h['reached']) for h in hits], [(iso(1), 'rate_limit_reached')])
        type_first = [self.cx('1', 1, 100.0, reached='rate_limit_reached'), self.cx('2', 2, 100.0)]
        self.assertEqual(len(limits.limit_hits(type_first, [], TABLE)), 1)

    def test_window_full_is_public_and_counted_by_window_length(self):
        hits = limits.limit_hits([self.cx('1', 1, 99.0), self.cx('2', 2, 100.0)], [], TABLE)
        facts = insights.cost_facts([], TABLE, hits=hits)
        self.assertEqual(next(f for f in facts['facts'] if f['id'] == 'limit_hits')['values']['limits'], [{'limit': 'five_hour', 'harness': 'codex', 'count': 1}])
        payload = report.build_report([self.cx('1', 1, 99.0)], {}, redact=True, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual(payload['limit_hits'][0]['reached'], 'window_full')


class RoundTwelve(RoundEleven):
    test_unchanged_counters_with_a_reached_snapshot_are_one_hit = None  # inherited helpers only
    test_quota_only_event_with_no_info_is_one_hit = None
    test_recovery_with_unchanged_counters_between_two_exhaustions_is_two_hits = None
    test_no_transition_adds_no_records = None
    test_statusline_cache_ignores_codex_quota_events = None
    test_resetless_claude_retry_after_the_known_reset_is_a_new_hit = None

    @staticmethod
    def two(five, week, reset=None):
        from test_why_codex import _limits, _window
        reset = reset or int((T0 + timedelta(hours=3)).timestamp())
        return _limits(primary=_window(five, 300, reset), secondary=_window(week, 10080, reset + 86400))

    def test_one_window_changing_while_another_stays_full_is_a_transition(self):
        h = self.history(self.calls((1, self.two(50.0, 100.0)), (2, self.two(99.0, 100.0)), (3, self.two(100.0, 100.0)), (4, self.two(40.0, 100.0))))
        # first row is usage; rows 2-4 are quota-only: 99 keeps the same full set (no event), 100 adds the 5-hour window, 40 removes it
        self.assertEqual(len(h.limit_events()), 2)
        hits = self.hits(h)
        self.assertEqual(sorted((x['window_minutes'], x['reached']) for x in hits), [(300, 'window_full'), (10080, 'window_full')])

    def test_full_window_with_no_reached_type_reaches_the_report_and_insights(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        from test_why_codex import _meta, _quota_call, _write_rollout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / 'codex' / 'rollout-c.jsonl', [_meta('c1'), _quota_call(stamp(1), 1, self.two(100.0, 20.0))])
            db = str(root / 'h.sqlite3')
            def run(*args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(['--db', db, *args])
                return code, out.getvalue()
            self.assertEqual(run('refresh', '--harness', 'codex', '--root', str(root / 'codex'))[0], 0)
            html = root / 'r.html'
            run('report', '--html', str(html), '--private')
            found = re.search(r'id="report-data"[^>]*>([^<]+)<', html.read_text(encoding='utf-8')).group(1)
            payload = json.loads(gzip.decompress(base64.b64decode(found)).decode())
            self.assertEqual([(x['reached'], x['window_minutes']) for x in payload['limit_hits']], [('window_full', 300)])
            facts = json.loads(run('insights', '--json')[1])['facts']
            self.assertEqual(next(f for f in facts if f['id'] == 'limit_hits')['values']['count'], 1)

    def test_an_expired_named_episode_does_not_suppress_a_later_full_window_hit(self):
        def cx(id, minute, reached, resets):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached,
                     'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': 100.0, 'resets_at': iso(resets)}]}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        recs = [cx('1', 1, 'rate_limit_reached', 400), cx('2', 302, None, 700)]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['reached']) for h in hits], [(iso(1), 'rate_limit_reached'), (iso(302), 'window_full')])


class OneStatePerSeries(RoundEleven):
    test_unchanged_counters_with_a_reached_snapshot_are_one_hit = None  # inherited helpers only
    test_quota_only_event_with_no_info_is_one_hit = None
    test_recovery_with_unchanged_counters_between_two_exhaustions_is_two_hits = None
    test_no_transition_adds_no_records = None
    test_statusline_cache_ignores_codex_quota_events = None
    test_resetless_claude_retry_after_the_known_reset_is_a_new_hit = None

    @staticmethod
    def cx(id, minute, reached, windows, limit_id='codex', session='c1'):
        quota = {'limit_id': limit_id, 'plan_type': 'plus', 'reached': reached, 'windows': windows}
        return obs(id, iso(minute), 1000, harness='codex', provider='openai', session=session, turn='ct', quota=quota)

    @staticmethod
    def w(percent, minutes=300, resets=500):
        return {'slot': 'primary' if minutes == 300 else 'secondary', 'minutes': minutes, 'used_percent': percent, 'resets_at': iso(resets)}

    def test_collector_keeps_a_stale_full_window_alive_so_a_second_instance_is_a_second_hit(self):
        h = self.history(self.calls((1, self.snap(100.0, hours=3)), (302, self.snap(100.0, hours=9))))
        self.assertEqual(len(h.limit_events()), 1)  # unchanged state, but the last full evidence is older than the window: kept alive
        self.assertEqual(len(self.hits(h)), 2)
        h2 = self.history(self.calls((1, self.snap(100.0)), (100, self.snap(100.0))))
        self.assertEqual(len(h2.limit_events()), 0)  # still inside the window: nothing to add

    def test_a_recovery_by_one_session_while_another_reported_reached(self):
        recs = [self.cx('1', 1, None, [self.w(100.0)], session='A'), self.cx('2', 2, 'rate_limit_reached', [self.w(100.0)], session='B'),
                self.cx('3', 3, None, [self.w(40.0, resets=500)], session='A'), self.cx('4', 4, 'rate_limit_reached', [self.w(100.0, resets=900)], session='A')]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['reached']) for h in hits], [(iso(1), 'rate_limit_reached'), (iso(4), 'rate_limit_reached')])

    def test_continuous_readings_of_a_full_window_are_one_hit_and_a_real_gap_is_two(self):
        h = self.history(self.calls(*[(m, self.snap(100.0)) for m in (1, 100, 200, 300, 302)]))
        self.assertEqual([e['ts'][11:16] for e in h.limit_events()], ['11:40', '13:20'])  # the reading before each half-window gap is retained
        self.assertEqual(len(self.hits(h)), 1)
        gap = self.history(self.calls((1, self.snap(100.0, hours=3)), (400, self.snap(100.0, hours=12))))
        self.assertEqual(len(self.hits(gap)), 2)

    def test_provisional_episode_migrates_into_the_first_full_window(self):
        weekly = lambda p: self.w(p, 10080, 900)
        recs = [self.cx('1', 1, 'rate_limit_reached', []), self.cx('2', 2, 'rate_limit_reached', [self.w(100.0)]),
                self.cx('3', 3, 'rate_limit_reached', [self.w(100.0), weekly(100.0)])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['window_minutes'], h['reached']) for h in hits],
                         [(iso(1), 300, 'rate_limit_reached'), (iso(3), 10080, 'window_full')])  # migrated hit keeps its time; the later weekly crossing is its own
        gap = [self.cx('1', 1, 'rate_limit_reached', []), self.cx('2', 2, 'rate_limit_reached', [self.w(100.0)]),
               self.cx('3', 400, 'rate_limit_reached', [self.w(100.0, resets=900)])]
        self.assertEqual(len(limits.limit_hits(gap, [], TABLE)), 2)

    def test_empty_windows_are_neutral_for_a_window_series(self):
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.w(100.0)]), self.cx('2', 2, 'rate_limit_reached', []),
                self.cx('3', 3, 'rate_limit_reached', [self.w(100.0)])]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def test_a_new_window_instance_with_unchanged_state_is_retained_by_the_collector(self):
        h = self.history(self.calls((1, self.snap(100.0, hours=5 / 60)), (6, self.snap(100.0, hours=400 / 60))))
        self.assertEqual(len(h.limit_events()), 1)
        self.assertEqual(len(self.hits(h)), 2)

    def test_irregular_spacing_keeps_continuity_and_a_real_gap_still_splits(self):
        h = self.history(self.calls(*[(m, self.snap(100.0)) for m in (0, 149, 301)]))
        self.assertEqual(len(self.hits(h)), 1)  # 149 is retained before the gap to 301, so no fake 301-minute gap
        gap = self.history(self.calls((0, self.snap(100.0, hours=3)), (149, self.snap(100.0, hours=3)), (460, self.snap(100.0, hours=12))))
        self.assertEqual(len(self.hits(gap)), 2)  # 460 - 149 = 311 > the window: a genuine gap

    def test_a_stale_provisional_episode_is_not_migrated_and_old_window_episodes_expire_everywhere(self):
        recs = [self.cx('1', 0, 'rate_limit_reached', []), self.cx('2', 1440, 'rate_limit_reached', [self.w(100.0, resets=1700)])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['window_minutes']) for h in hits], [(iso(0), None), (iso(1440), 300)])
        self.assertEqual([h['turn'] for h in hits], [('codex', 'c1', 'ct')] * 2)
        windowless = [self.cx('1', 0, None, [self.w(100.0, resets=400)]), self.cx('2', 1440, 'rate_limit_reached', [])]
        hits = limits.limit_hits(windowless, [], TABLE)
        self.assertEqual([(h['at'], h['reached'], h['window_minutes']) for h in hits], [(iso(0), 'window_full', 300), (iso(1440), 'rate_limit_reached', None)])

    def test_a_credits_episode_never_coalesces_with_a_time_window(self):
        credits = 'workspace_owner_credits_depleted'
        recs = [self.cx('1', 1, credits, [self.w(50.0)]), self.cx('2', 2, credits, [self.w(100.0)])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['window_minutes'], h['reached']) for h in hits], [(None, credits), (300, 'window_full')])

    def test_a_named_five_hour_window_never_suppresses_a_weekly_crossing(self):
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.w(100.0), self.w(50.0, 10080, 900)]),
                self.cx('2', 2, 'rate_limit_reached', [self.w(100.0), self.w(100.0, 10080, 900)])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['window_minutes'], h['reached']) for h in hits], [(300, 'rate_limit_reached'), (10080, 'window_full')])

    def test_two_limit_ids_with_identical_full_states_both_hit(self):
        recs = [self.cx('1', 1, None, [self.w(100.0)], limit_id='codex'), self.cx('2', 1, None, [self.w(100.0)], limit_id='codex_bengalfox')]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 2)


class Template(unittest.TestCase):
    def test_turn_row_variables_are_declared(self):
        # The script is strict: assigning to an undeclared name throws on the first attributed turn and the report never initializes.
        page = (Path(report.__file__).with_name('report_template.html')).read_text(encoding='utf-8')
        m = re.search(r"list\.forEach\(\(p,i\)=>\{(const tr=.*?);\[String\(i\+1\)", page)
        self.assertIsNotNone(m)
        statement = m.group(1)
        self.assertRegex(statement, r"^const tr=el\('tr',undefined,'prompt-row'\),c=.*,n=.*;tr\.id=")


class RoundSix(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / 'state' / 'h.sqlite3')
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(['--db', self.db, *args])
            return code, out.getvalue()
        self.run_cli = run

    def refresh(self, rows, sub=None):
        write(self.root / 'logs', rows)
        if sub:
            path = self.root / 'logs' / 'proj' / 's1' / 'subagents' / 'agent-x1.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(''.join(json.dumps(r) + '\n' for r in sub))
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude', '--root', str(self.root / 'logs'))[0], 0)

    def payload(self, *extra):
        out = self.root / 'r.html'
        self.run_cli('report', '--html', str(out), *extra)
        found = re.search(r'id="report-data"[^>]*>([^<]+)<', out.read_text(encoding='utf-8')).group(1)
        return json.loads(gzip.decompress(base64.b64decode(found)).decode())

    def test_statusline_cache_does_not_count_a_rejection(self):
        from tokenatlas import statusline
        write(self.root / 'logs', [user('t1', 0), call('a', 1, 5000), rejected('r1', 2)])
        with History(Path(self.db)) as h:
            h.refresh('claude', self.root / 'logs')
            cache = statusline.build_cache(h, T0 + timedelta(hours=1))
        days = cache['days'].values()
        self.assertEqual((sum(d['requests'] for d in days), sum(d['incomplete'] for d in days)), (1, 0))

    def test_top_marks_only_hits_inside_the_cli_filters(self):
        self.refresh([user('t1', 0), call('a', 1, 5000), user('t2', 30), call('c', 31, 1000), rejected('r1', 40), call('d', 45, 1000)])
        def marked(*extra):
            prompts = {p['turn_id']: p for p in json.loads(self.run_cli('top', '--json', *extra)[1])['prompts']}
            return 'limit_hit' in prompts['t2']
        self.assertTrue(marked())
        self.assertTrue(marked('--end', stamp(41)))
        self.assertFalse(marked('--end', stamp(40)))  # the hit itself is at the exclusive end
        self.assertFalse(marked('--start', stamp(41)))  # only the later call of turn t2 is in range
        self.assertTrue(marked('--start', stamp(40)))

    def test_cards_and_hits_agree_when_a_date_filter_cuts_through_a_subagent_turn(self):
        def sub_call(request, minute):
            return dict(call(request, minute, 20000, sidechain=True), agentId='x1', attributionAgent='x1')
        self.refresh([user('t1', 0), call('a', 1, 1000), user('t2', 30), call('c', 31, 1000), rejected('r1', 50)],
                     sub=[sub_call('s1a', 35), sub_call('s1b', 45)])
        full = self.payload()
        self.assertEqual(len(full['limit_hits']), 1)
        cut = self.payload('--start', stamp(40))
        top = cut['limit_hits'][0]['window']['top']
        self.assertTrue(top and top[0]['prompt'] is not None, top)
        cols = cut['columns']
        self.assertIn(top[0]['prompt'], cols['prompt'])  # a card exists for the ranked turn


class Cli(unittest.TestCase):
    def test_top_marks_the_turn_that_hit_a_limit(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'logs', session_rows())
            db = str(root / 'state' / 'h.sqlite3')
            def run(*args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(['--db', db, *args])
                return code, out.getvalue()
            self.assertEqual(run('refresh', '--harness', 'claude', '--root', str(root / 'logs'))[0], 0)
            code, text = run('top', '--json')
            prompts = {p['turn_id']: p for p in json.loads(text)['prompts']}
            self.assertEqual(prompts['t2']['limit_hit']['reached'], 'five_hour')
            self.assertEqual(prompts['t2']['limit_hit']['window_minutes'], 300)
            self.assertNotIn('limit_hit', prompts['t1'])
            self.assertEqual(prompts['t2']['requests'], 1)  # the three rejected retries are not requests
            self.assertIn('[Hit the 5-hour limit]', run('top')[1])
            def payload(*extra):
                out = root / 'r.html'
                run('report', '--html', str(out), *extra)
                page = out.read_text(encoding='utf-8')
                found = re.search(r'id="report-data"[^>]*>([^<]+)<', page).group(1)
                return json.loads(gzip.decompress(base64.b64decode(found)).decode())
            self.assertEqual(len(payload().get('limit_hits', [])), 1)
            self.assertEqual(len(payload('--harness', 'claude').get('limit_hits', [])), 1)
            self.assertNotIn('limit_hits', payload('--harness', 'codex'))
            self.assertNotIn('limit_hits', payload('--start', '2026-09-05T00:00:00+00:00'))
            self.assertEqual(len(payload('--start', '2026-09-01T00:00:00+00:00', '--end', '2026-09-10T00:00:00+00:00').get('limit_hits', [])), 1)
            code, text = run('insights', '--json')
            self.assertTrue(any(f['id'] == 'limit_hits' for f in json.loads(text)['facts']))


class CliScope(unittest.TestCase):
    def test_scoped_reports_keep_rejection_only_turns_and_filter_by_provider(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        from test_why_codex import _limits, _meta, _quota_call, _window, _write_rollout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'logs', [user('t1', 0), call('a', 1, 5000), user('t2', 30), rejected('r1', 31)])
            reset = int((T0 + timedelta(hours=2)).timestamp())
            _write_rollout(root / 'codex' / 'rollout-c.jsonl', [_meta('c1'), _quota_call(stamp(5), 1, _limits(
                primary=_window(100.0, 300, reset), rate_limit_reached_type='rate_limit_reached'))])
            db = str(root / 'state' / 'h.sqlite3')
            def run(*args):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return main(['--db', db, *args])
            self.assertEqual(run('refresh', '--harness', 'claude', '--root', str(root / 'logs')), 0)
            self.assertEqual(run('refresh', '--harness', 'codex', '--root', str(root / 'codex')), 0)
            def hits(*extra):
                out = root / 'r.html'
                run('report', '--html', str(out), *extra)
                found = re.search(r'id="report-data"[^>]*>([^<]+)<', out.read_text(encoding='utf-8')).group(1)
                return [h['harness'] for h in json.loads(gzip.decompress(base64.b64decode(found)).decode()).get('limit_hits', [])]
            self.assertEqual(sorted(hits()), ['claude', 'codex'])
            for extra in (['--project', '/work/app'], ['--session', 's1'], ['--session', 'claude:s1'], ['--turn', 't2']):
                self.assertEqual(hits(*extra), ['claude'], extra)
            self.assertEqual(hits('--provider', 'anthropic'), ['claude'])
            self.assertEqual(hits('--provider', 'openai'), ['codex'])


if __name__ == '__main__':
    unittest.main()
