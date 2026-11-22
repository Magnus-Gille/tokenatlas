"""Number labels in JSON output (TOKEN-28): logged / derived / estimate / lower_bound / unknown. Synthetic data only."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tokenatlas import __main__ as cli
from tokenatlas import labels

SPLIT = {'ephemeral_5m_input_tokens': 5, 'ephemeral_1h_input_tokens': 0}


def request(n, model, ts, turn='u1', split=True, stop='end_turn'):
    usage = {'input_tokens': 10, 'cache_read_input_tokens': 20, 'output_tokens': 50}
    usage.update(cache_creation=SPLIT, cache_creation_input_tokens=5) if split else usage.update(cache_creation_input_tokens=5)
    return {'type': 'assistant', 'uuid': f'a{n}', 'requestId': f'r{n}', 'sessionId': turn, 'cwd': '/work/app', 'timestamp': ts,
            'message': {'id': f'm{n}', 'model': model, 'role': 'assistant', 'stop_reason': stop, 'usage': usage}}


def prompt(turn, ts):
    return {'type': 'user', 'uuid': turn, 'sessionId': turn, 'cwd': '/work/app', 'timestamp': ts, 'message': {'role': 'user', 'content': 'hi'}}


class TopLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        projects = root / '.claude/projects/p'
        projects.mkdir(parents=True)
        rows = {
            'priced': [prompt('priced', '2026-09-03T09:59:00Z'), request(1, 'claude-sonnet-4-5', '2026-09-03T10:00:00Z', 'priced')],
            'unpriced': [prompt('unpriced', '2026-09-04T09:59:00Z'), request(2, 'mystery-model', '2026-09-04T10:00:00Z', 'unpriced')],
            'partial': [prompt('partial', '2026-09-06T09:59:00Z'), request(5, 'claude-sonnet-4-5', '2026-09-06T10:00:00Z', 'partial', stop=None)],
            'mixed': [prompt('mixed', '2026-09-05T09:59:00Z'), request(3, 'claude-sonnet-4-5', '2026-09-05T10:00:00Z', 'mixed'),
                      request(4, 'mystery-model', '2026-09-05T10:01:00Z', 'mixed')],
        }
        for name, lines in rows.items():
            (projects / f'{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in lines))
        env = mock.patch.dict(os.environ, HOME=str(root), USERPROFILE=str(root), XDG_STATE_HOME=str(root / 'state'), TOKENATLAS_NO_BROWSER='1',
                              CLAUDE_CONFIG_DIR=str(root / '.claude'), CODEX_HOME=str(root / 'no-codex'),
                              PI_CODING_AGENT_DIR=str(root / 'no-pi'), XDG_DATA_HOME=str(root / 'no-data'))
        env.start()
        cls.addClassCleanup(env.stop)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.db = root / 'history.sqlite3'
        cls.run_cli('refresh', '--all')
        code, out = cls.run_cli('top', '--json', '-n', '10')
        assert code == 0, out
        cls.top = json.loads(out)
        cls.by_session = {p['session']: p for p in cls.top['prompts']}

    @classmethod
    def run_cli(cls, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(['--db', str(cls.db), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_a_known_price_keeps_its_default_label(self):
        row = self.by_session['priced']
        self.assertIsNotNone(row['cost'])
        self.assertTrue(row['cost_complete'])
        self.assertNotIn('cost', row.get('labels', {}))  # the default for cost is derived
        self.assertEqual(self.top['labels']['fields']['prompts[].cost'], 'derived')

    def test_an_unknown_price_is_unknown_and_stays_null(self):
        row = self.by_session['unpriced']
        self.assertIsNone(row['cost'])
        self.assertEqual(row['labels']['cost'], 'unknown')

    def test_an_incomplete_cost_is_a_lower_bound(self):
        row = self.by_session['mixed']
        self.assertIsNotNone(row['cost'])
        self.assertFalse(row['cost_complete'])
        self.assertEqual(row['labels']['cost'], 'lower_bound')

    def test_an_unfinished_request_makes_the_counts_a_lower_bound(self):
        row = self.by_session['partial']
        self.assertTrue(row['credits_lower_bound'])
        self.assertEqual(row['labels']['tokens'], 'lower_bound')
        self.assertEqual(row['labels']['total_tokens'], 'lower_bound')
        self.assertNotIn('tokens', self.by_session['priced'].get('labels', {}))

    def test_token_counts_are_logged(self):
        self.assertEqual(self.top['labels']['fields']['prompts[].tokens.*'], 'logged')
        self.assertEqual(self.top['labels']['fields']['prompts[].total_tokens'], 'logged')

    def test_every_numeric_field_of_the_output_has_a_label(self):
        missing = {p for p in labels.leaf_paths(self.top) if labels.lookup('top', p) is None}
        self.assertEqual(missing, set())

    def test_annotation_changes_no_number(self):
        _, labelled = self.run_cli('top', '--json', '-n', '10')
        with mock.patch.object(labels, 'annotate', lambda command, data: data):
            _, plain = self.run_cli('top', '--json', '-n', '10')
        self.assertEqual(labels.strip(json.loads(labelled)), json.loads(plain))
        self.assertIn('labels', json.loads(labelled))
        data = json.loads(plain)
        before = copy.deepcopy(data)
        labels.annotate('top', data)
        self.assertEqual(data, before, 'annotate must not mutate its input')


class Contract(unittest.TestCase):
    def test_nested_shapes_of_optional_blocks_are_covered(self):
        sample = {'total_prompts': 1, 'unattributed_observations': 0, 'text_store': {'added': 1, 'evicted': 0, 'kept': 1},
                  'prompts': [{'quota_share': {'percent': 3.0, 'exact_percent': 3.1, 'window_minutes': 300, 'lower_bound': True,
                                               'calibration': {'budget_usd': 5.0, 'readings': 3, 'spread': [1, 2], 'by_source': {'limit_hit': 1, 'statusline': 2}}},
                               'limit_hit': {'window': {'minutes': 300}}, 'context': {'activity': {'edits': 1, 'shell': 2}, 'inputs': {'count': 4}},
                               'tokens': {'output': 1}, 'total_tokens': 1, 'requests': 1, 'cost': 1.0}]}
        self.assertEqual({p for p in labels.leaf_paths(sample) if labels.lookup('top', p) is None}, set())

    def test_quota_share_label_follows_its_own_state(self):
        rows = [{'cost': 1.0, 'credits': 1.0, 'quota_share': {'label': 'observed'}},
                {'cost': 1.0, 'credits': 1.0, 'quota_share': {'label': 'calibrated', 'lower_bound': True}},
                {'cost': 1.0, 'credits': 1.0, 'quota_share': {'label': 'estimate'}}]
        out = labels.annotate('top', {'total_prompts': 3, 'prompts': rows})['prompts']
        self.assertEqual([r.get('labels', {}).get('quota_share') for r in out], ['derived', 'lower_bound', None])


    def test_errors_receipts_and_unregistered_commands_are_left_alone(self):
        for command, data in (('top', {'forgotten': '/x'}), ('top', {'ok': False, 'error': {'code': 'usage'}}), ('doctor', {'a': 1})):
            self.assertIs(labels.annotate(command, data), data)

    def test_worst_label_wins(self):
        self.assertEqual(labels.worst('logged', 'derived'), 'derived')
        self.assertEqual(labels.worst('derived', 'estimate', 'lower_bound'), 'lower_bound')
        self.assertEqual(labels.worst('lower_bound', 'unknown', 'logged'), 'unknown')

    def test_legacy_vocabulary_maps_onto_the_new_one(self):
        self.assertEqual({k: labels.legacy(k) for k in ('measured', 'computed', 'estimate', 'absent', None)},
                         {'measured': 'logged', 'computed': 'derived', 'estimate': 'estimate', 'absent': 'unknown', None: 'unknown'})
        self.assertTrue(set(labels.LEGACY.values()) <= set(labels.VOCABULARY))


if __name__ == '__main__':
    unittest.main()
