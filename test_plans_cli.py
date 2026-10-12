import json
import unittest

from tokenatlas.history import History, _encode
from test_fresh_report import Base


PRICES = {
    'schema': 1,
    'retrieved_on': '2026-09-01',
    'unit': 'per_million_tokens',
    'provider_aliases': {},
    'local_providers': [],
    'models': [
        {'provider': 'openai', 'model': 'gpt-plan', 'aliases': [], 'currency': 'USD',
         'input': 1, 'cache_read': 1, 'cache_write': 1, 'output': 1, 'free': False,
         'modifiers': {}, 'long_context': None, 'source_url': 'synthetic://plans',
         'retrieved_on': '2026-09-01'},
    ],
}


def row(identifier, stamp, turn, *, plan=None, model='gpt-plan', session='s', fresh=1_000_000):
    return {
        'v': 1, 'kind': 'usage_observation', 'id': identifier, 'id_synthetic': False,
        'ts': stamp, 'provider': 'openai', 'harness': 'codex', 'machine': 'm1',
        'session': session, 'agent': 'main', 'thread_kind': 'main', 'parent_session': None,
        'turn_id': turn, 'turn_confidence': 'derived', 'model': model,
        'project_id': '/work/plans', 'project_label': 'plans', 'effort': None,
        'origin': 'cli', 'tariff': None,
        'quota': {'plan_type': plan} if plan is not None else None,
        'flags': None, 'tokens': {'fresh_input': fresh, 'cache_read': 0, 'cache_write': 0,
                                  'output': 0, 'reasoning': 0},
        'raw_usage': {}, 'duration_ms': None, 'accounting_basis': 'request_top_level',
        'billing_verified': False, 'warnings': [], 'confidence': {}, 'complete': True,
        'output_final': None,
    }


class PlansCli(Base):
    def insert(self, rows):
        with History(self.db) as history:
            history.connection.execute('BEGIN')
            history._strings, history._values = {}, {}
            for item in rows:
                history._insert(history.connection, _encode(item))
            history._bump(history.connection)
            history.connection.commit()

    def setUp(self):
        super().setUp()
        self.state.mkdir(parents=True, exist_ok=True)
        self.prices = self.state / 'prices.json'
        self.prices.write_text(json.dumps(PRICES))

    def top(self, *args):
        return self.run_cli('top', '--prices', str(self.prices), *args)

    def insights(self, *args):
        return self.run_cli('insights', '--prices', str(self.prices), *args)

    def test_top_plan_filter_inherits_snapshot_outside_selected_window(self):
        self.insert([
            row('snap-pro', '2026-09-03T09:00:00+00:00', None, plan='PRO'),
            row('pro-turn', '2026-09-03T10:00:00+00:00', 'pro-turn'),
            row('snap-team', '2026-09-03T11:00:00+00:00', None, plan='TEAM'),
            row('team-turn', '2026-09-03T12:00:00+00:00', 'team-turn'),
        ])
        code, out, err = self.top('--plan', '  PRO ', '--start', '2026-09-03T09:30:00+00:00',
                                  '--end', '2026-09-03T10:30:00+00:00', '--json')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual([p['turn_id'] for p in result['prompts']], ['pro-turn'])
        self.assertEqual(result['prompts'][0]['plan'], 'pro')

        code, out, err = self.top('--plan', 'TEAM', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual([p['turn_id'] for p in json.loads(out)['prompts']], ['team-turn'])

    def test_top_budget_uses_same_inherited_plan_as_quota_show(self):
        self.insert([row('snapshot', '2026-09-03T09:00:00+00:00', None, plan='pro'),
                     row('request', '2026-09-03T10:00:00+00:00', 'inherited')])
        code, out, err = self.run_cli('quota', 'set', '--harness', 'codex', '--plan', 'pro',
                                      '--window', '5h', '--budget-usd', '100')
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli('quota', 'show', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['derived'][0]['plan'], 'pro')
        code, out, err = self.top('--json')
        self.assertEqual(code, 0, err)
        turn = next(p for p in json.loads(out)['prompts'] if p['turn_id'] == 'inherited')
        self.assertEqual(turn['plan'], 'pro')
        self.assertEqual(turn['quota_share']['label'], 'calibrated')
        self.assertEqual(turn['quota_share']['delta_percent'], 1.0)

    def test_top_marks_unknown_and_mixed_plans_and_text_uses_labels(self):
        self.insert([
            row('unknown', '2026-09-03T10:00:00+00:00', 'unknown-turn', session='unknown'),
            row('mixed-pro', '2026-09-03T11:00:00+00:00', 'mixed-turn', plan='pro', session='mixed'),
            row('mixed-team', '2026-09-03T11:01:00+00:00', 'mixed-turn', plan='team', session='mixed'),
        ])
        code, out, err = self.top('--by', 'tokens', '--json')
        self.assertEqual(code, 0, err)
        plans = {p['turn_id']: p['plan'] for p in json.loads(out)['prompts']}
        self.assertEqual(plans, {'unknown-turn': 'unknown', 'mixed-turn': 'mixed'})

        code, out, err = self.top('--by', 'tokens')
        self.assertEqual(code, 0, err)
        self.assertIn('plan', out.splitlines()[0])
        self.assertIn('Unknown', out)

    def test_insights_plan_partition_is_additive_and_windowed(self):
        self.insert([
            row('pro', '2026-09-03T10:00:00+00:00', 'pro-turn', plan='pro'),
            row('team', '2026-09-03T11:00:00+00:00', 'team-turn', plan='team'),
            row('unknown', '2026-09-03T12:00:00+00:00', 'unknown-turn', model='unpriced', session='unknown'),
        ])
        code, out, err = self.insights('--json', '--start', '2026-09-03T10:30:00+00:00')
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertIn('subscriptions', result)
        plans = {entry['plan']: entry for entry in result['subscriptions']['plans']}
        self.assertEqual(set(plans), {'team', 'unknown'})
        self.assertEqual((plans['team']['observations'], plans['team']['turns'], plans['team']['priced']), (1, 1, 1))
        self.assertEqual((plans['unknown']['observations'], plans['unknown']['turns'], plans['unknown']['priced'], plans['unknown']['unpriced']), (1, 1, 0, 1))
        self.assertEqual(result['requests'], 2)
        self.assertEqual(result['subscriptions']['note'].count('counted once'), 1)

        code, out, err = self.insights('--start', '2026-09-03T11:30:00+00:00')
        self.assertEqual(code, 0, err)
        self.assertIn('Unknown:', out)
        self.assertIn('priced cost n/a', out)
        self.assertIn('plan turn counts may overlap', out)


if __name__ == '__main__':
    unittest.main()
