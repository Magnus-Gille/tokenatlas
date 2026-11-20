import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import cache_spend, pricing
from tokenatlas.__main__ import main
from tokenatlas.history import History

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
TABLE = pricing.load_prices()
MODEL = 'claude-sonnet-5'  # $2 input, $2.5 write 5m, $4 write 1h, $0.2 read per Mtok in the packaged table


def req(n, minutes, read=0, write=0, hour=None, model=MODEL, session='s1', agent='main', effort='high', speed='standard', fresh=10):
    split = {'ephemeral_5m_input_tokens': 0 if hour is None else write - hour, 'ephemeral_1h_input_tokens': hour or 0}
    if hour is None:
        split = {'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0}
    return {'id': f'r{n}', 'ts': (T0 + timedelta(minutes=minutes)).isoformat(), 'harness': 'claude', 'provider': 'anthropic', 'model': model,
            'session': session, 'agent': agent, 'effort': effort, 'tariff': {'speed': speed}, 'project_label': 'app', 'complete': True,
            'tokens': {'fresh_input': fresh, 'cache_read': read, 'cache_write': write, 'output': 5, 'reasoning': None},
            'raw_usage': {'cache_creation': split}, 'accounting_basis': 'request_top_level', 'warnings': [], 'confidence': {}}


def causes(records, **kw):
    result = cache_spend.analyze(records, TABLE, **kw)
    return {c['cause']: c['requests'] for c in result['causes'] if c['requests']}, result


class ClassifyTests(unittest.TestCase):
    def test_every_cause(self):
        records = [
            req(1, 0, write=50000),                                     # cold start
            req(2, 1, read=50000, write=500),                           # healthy: reads the prefix
            req(3, 100, write=52000),                                   # more than an hour later
            req(4, 107, write=52000),                                   # 7 minutes after a 5-minute write
            req(5, 108, write=52000, model='claude-opus-5-5'),          # model switch, no pause
            req(6, 109, write=52000, model='claude-opus-5-5', effort='low'),
            req(7, 110, write=52000, model='claude-opus-5-5', effort='low', speed='fast'),
            req(8, 111, write=52000, model='claude-opus-5-5', effort='low', speed='fast'),  # nothing changed: other
        ]
        found, _ = causes(records)
        self.assertEqual(found, {'cold_start': 1, 'idle_over_1h': 1, 'idle_5m_to_1h': 1, 'model_switch': 1,
                                 'effort_switch': 1, 'speed_switch': 1, 'other': 1})

    def test_healthy_thread_has_no_recache(self):
        records = [req(1, 0, write=3000)] + [req(n, n, read=3000 + 400 * n, write=400) for n in range(2, 8)]
        found, result = causes(records)
        self.assertEqual(found, {})  # the first write is below the minimum
        self.assertEqual(result['recache_cost_usd'], 0)

    def test_one_hour_write_survives_a_short_pause(self):
        records = [req(1, 0, write=50000, hour=50000), req(2, 20, write=52000, hour=52000)]
        found, _ = causes(records)
        self.assertEqual(found, {'cold_start': 1, 'other': 1})  # 20 minutes is inside a 1-hour TTL

    def test_pause_wins_over_model_switch(self):
        found, _ = causes([req(1, 0, write=50000), req(2, 180, write=50000, model='claude-opus-5-5')])
        self.assertEqual(found, {'cold_start': 1, 'idle_over_1h': 1})

    def test_subagent_is_its_own_thread(self):
        records = [req(1, 0, write=50000), req(2, 1, write=50000, agent='sub1')]
        found, _ = causes(records)
        self.assertEqual(found, {'cold_start': 2})

    def test_cost_and_unknown_price(self):
        records = [req(1, 0, write=1_000_000), req(2, 1, write=1_000_000, model='unpriced-model', session='s2')]
        found, result = causes(records)
        cold = next(c for c in result['causes'] if c['cause'] == 'cold_start')
        self.assertEqual(cold['requests'], 2)
        self.assertAlmostEqual(cold['cost_usd'], 2.5)  # 1M tokens of 5-minute write; the unpriced one is not guessed as 0
        self.assertEqual(cold['unpriced_requests'], 1)
        self.assertEqual(result['applicable']['unpriced_requests'], 1)

    def test_resume_tax_ranks_idle_sessions_and_hides_project(self):
        records = [req(1, 0, write=50000, session='a'), req(2, 200, write=50000, session='a'),
                   req(3, 0, write=50000, session='b'), req(4, 300, write=900000, session='b')]
        _, result = causes(records)
        self.assertEqual([s['session'] for s in result['resume_tax']], ['b', 'a'])
        self.assertNotIn('project', result['resume_tax'][0])
        self.assertEqual(cache_spend.analyze(records, TABLE, private=True)['resume_tax'][0]['project'], 'app')

    def test_ttl_what_if(self):
        # Four requests 10 minutes apart: the 5-minute TTL rewrites the context every time, the 1-hour TTL reads it.
        records = [req(n, 10 * n, read=0 if n == 0 else 100000 * n, write=100000 if n == 0 else 100000) for n in range(4)]
        _, result = causes(records)
        w = result['ttl_what_if']
        self.assertEqual(w['cheaper'], '1h')
        self.assertGreater(w['saving_1h_vs_5m_usd'], 0)
        # Requests 1 minute apart: both TTLs read, the 1-hour write premium makes 5 minutes cheaper.
        quick = [req(n, n, read=0 if n == 0 else 100000 * n, write=100000) for n in range(4)]
        self.assertEqual(causes(quick)[1]['ttl_what_if']['cheaper'], '5m')


    def test_window_keeps_the_request_before_it_as_context(self):
        records = [req(1, 0, write=50000), req(2, 1, read=50000, write=500), req(3, 2, write=52000)]
        start = T0 + timedelta(minutes=2)
        found, result = causes(records, start=start)
        self.assertEqual(found, {'other': 1})  # not a cold start: the thread began before the window
        self.assertEqual(result['applicable']['requests'], 1)

    def test_one_hour_write_followed_by_a_read_is_not_a_five_minute_expiry(self):
        records = [req(1, 0, write=50000, hour=50000), req(2, 1, read=50000), req(3, 11, write=52000)]
        self.assertEqual(causes(records)[0], {'cold_start': 1, 'other': 1})

    def test_unknown_counters_are_not_zero(self):
        bad = req(1, 0, write=50000)
        bad['tokens']['cache_read'] = None
        self.assertEqual(causes([bad])[0], {})

    def test_ambiguous_identity_is_left_out(self):
        bad = dict(req(1, 0, write=50000), id_synthetic=True)
        found, result = causes([bad])
        self.assertEqual(found, {})
        self.assertEqual(result['applicable']['excluded_ambiguous_requests'], 1)

    def test_missing_model_is_not_a_switch(self):
        first, second = req(1, 0, write=50000), req(2, 1, write=50000)
        second['model'] = None
        self.assertEqual(causes([first, second])[0], {'cold_start': 1, 'other': 1})

    def test_prefix_miss_within_the_ttl_is_not_replayed_as_a_read(self):
        records = [req(1, 0, write=100000), req(2, 1, write=100000)]  # the second read nothing: the prefix really changed
        w = causes(records)[1]['ttl_what_if']
        self.assertAlmostEqual(w['cost_5m_usd'], 2 * 100000 * 2.5 / 1e6)

    def test_missing_one_hour_rate_is_unpriced_in_the_what_if(self):
        table = {**TABLE, 'models': [dict(m, cache_write_1h=None) if m['model'] == MODEL else m for m in TABLE['models']]}
        result = cache_spend.analyze([req(1, 0, write=50000)], table)
        self.assertIsNone(result['ttl_what_if']['cheaper'])
        self.assertEqual(result['ttl_what_if']['unpriced_requests'], 1)

    def test_by_model_lists_tokens(self):
        _, result = causes([req(1, 0, write=50000)])
        self.assertEqual(result['by_model'][0]['write_tokens'], 50000)
        self.assertIn('by model', cache_spend.render_text(result))

    def test_other_harness_is_not_applicable(self):
        other = dict(req(1, 0, write=50000), harness='codex')
        _, result = causes([other])
        self.assertEqual(result['applicable']['requests'], 0)
        self.assertEqual(result['applicable']['not_applicable_requests_with_cache_writes'], 1)


class CommandTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_json_and_text_from_a_real_history(self):
        tmp = Path(tempfile.mkdtemp())
        root = tmp / 'projects' / 'p'
        root.mkdir(parents=True)
        rows = [{'type': 'user', 'uuid': 'u', 'message': {'role': 'user', 'content': 'PRIVATE'}}]
        for n, minutes in enumerate((0, 1, 200)):
            rows.append({'type': 'assistant', 'requestId': f'r{n}', 'sessionId': 's', 'cwd': '/w/app',
                         'timestamp': (T0 + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                         'message': {'id': f'm{n}', 'model': MODEL, 'stop_reason': 'end_turn',
                                     'usage': {'input_tokens': 10, 'cache_read_input_tokens': 0 if n != 1 else 60000, 'output_tokens': 5,
                                               'cache_creation_input_tokens': 60000 if n != 1 else 100,
                                               'cache_creation': {'ephemeral_5m_input_tokens': 60000 if n != 1 else 100, 'ephemeral_1h_input_tokens': 0}}}})
        (root / 's.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        db = tmp / 'h.db'
        with History(db) as history:
            history.refresh('claude', tmp / 'projects')
        code, out, err = self.run_cli('--db', str(db), 'cache', '--json')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['schema'], 'tokenatlas.cache/1')
        found = {c['cause']: c['requests'] for c in data['causes'] if c['requests']}
        self.assertEqual(found, {'cold_start': 1, 'idle_over_1h': 1})
        self.assertNotIn('PRIVATE', out)
        code, out, _ = self.run_cli('--db', str(db), 'cache')
        self.assertEqual(code, 0)
        self.assertIn('idle_over_1h', out)
        self.assertEqual(self.run_cli('--db', str(db), 'cache', '--share', '2')[0], 2)
        self.assertEqual(self.run_cli('--db', str(db), 'cache', '--start', '2030-01-01')[0], 4)  # no data


if __name__ == '__main__':
    unittest.main()
