import json
import unittest

from tokenatlas.efficiency import facts, rows_from_records, scenario


def row(ts='2026-10-10T01:00:00+00:00', **changes):
    value = {
        'ts': ts,
        'tokens': {'fresh_input': 10, 'cache_read': 20, 'cache_write': 5,
                   'output': 7, 'reasoning': 3},
        'prompt': 0, 'project_id': 'Project 001', 'complete': True,
        'id_synthetic': False, 'thread_kind': 'main', 'turn_confidence': 'observed',
        'efficiency_auto_review': False, 'efficiency_rolled_up': False,
        # Must never cross the output boundary.
        'session': 'secret-session', 'model': 'private-model', 'title': 'private-title',
    }
    value.update(changes)
    return value


class EfficiencyTests(unittest.TestCase):
    def get_fact(self, bundle, name):
        return next(f for f in bundle['facts'] if f['id'] == name)

    def test_arithmetic_and_reasoning_is_not_added_to_total(self):
        bundle = facts([row()], start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T02:00:00Z', context_threshold=30)
        self.assertEqual(bundle['totals']['tokens'],
                         {'fresh_input': 10, 'cache_read': 20, 'cache_write': 5, 'output': 7})
        self.assertEqual(bundle['totals']['known_tokens'], 42)
        self.assertEqual(bundle['totals']['known_input'], 35)
        self.assertEqual(bundle['totals']['complete'], True)
        concentration = self.get_fact(bundle, 'token_concentration')
        self.assertEqual((concentration['numerator'], concentration['denominator'], concentration['share']), (42, 42, 1))
        context = self.get_fact(bundle, 'context_volume')
        self.assertEqual((context['numerator'], context['denominator']), (35, 35))
        self.assertEqual(context['values']['median'], 35)
        self.assertEqual(bundle['window']['start'], '2026-10-10T00:00:00.000Z')

    def test_unknown_unlinked_auto_and_synthetic_coverage(self):
        rows = [
            row(prompt=None),
            row('2026-10-10T01:10:00Z', tokens={'fresh_input': None, 'cache_read': None,
                'cache_write': None, 'output': None, 'reasoning': None}, complete=False),
            row('2026-10-10T01:20:00Z', id_synthetic=True),
            row('2026-10-10T01:30:00Z', efficiency_auto_review=True),
        ]
        bundle = facts(rows, start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T02:00:00Z', context_threshold=1)
        self.assertEqual(bundle['coverage'], {'requests': 3, 'synthetic_excluded': 1,
                         'incomplete_requests': 1, 'unlinked_requests': 1,
                         'rolled_up_requests': 0, 'derived_requests': 0})
        self.assertEqual(bundle['totals']['requests'], 3)
        self.assertFalse(bundle['totals']['complete'])
        self.assertEqual(self.get_fact(bundle, 'context_volume')['values']['excluded_unknown_input'], 1)
        concentration = self.get_fact(bundle, 'token_concentration')
        self.assertEqual(concentration['values']['unlinked']['requests'], 1)
        self.assertEqual(concentration['values']['auto_review']['requests'], 1)
        self.assertEqual(concentration['denominator'], 0)  # linked incomplete work has no known counters

    def test_inherited_turns_are_numeric_and_safe_project_contributors(self):
        records = [
            {'ts': '2026-10-10T00:00:00Z', 'harness': 'claude', 'id': '01', 'thread_kind': 'main',
             'session': 'p', 'turn_id': 'first', 'parent_session': None, 'agent': None,
             'turn_confidence': 'observed', 'tokens': {'fresh_input': 1, 'cache_read': 0, 'cache_write': 0,
                 'output': 1, 'reasoning': 0}, 'complete': True, 'id_synthetic': False,
             'project_id': '/private/customer/repo', 'efficiency_rolled_up': False},
            {'ts': '2026-10-10T00:01:00Z', 'harness': 'claude', 'id': '02', 'thread_kind': 'subagent',
             'session': 'p', 'turn_id': None, 'parent_session': None, 'agent': 'worker',
             'turn_confidence': 'derived', 'tokens': {'fresh_input': 2, 'cache_read': 0, 'cache_write': 0,
                 'output': 2, 'reasoning': 0}, 'complete': True, 'id_synthetic': False,
             'project_id': '/private/customer/repo', 'efficiency_rolled_up': True},
        ]
        selected = rows_from_records(records)
        self.assertEqual(selected[0]['project_id'], '\x01p001')
        self.assertEqual(selected[0]['prompt'], selected[1]['prompt'])
        self.assertTrue(selected[1]['efficiency_rolled_up'])
        bundle = facts([{**r, 'efficiency_project_id': r['project_id']} for r in selected],
                       start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T02:00:00Z')
        change = self.get_fact(bundle, 'token_change')
        self.assertEqual(change['values']['projects'][0]['project'], 'Project 001')
        serialized = json.dumps(bundle)
        for secret in ('/private/', 'customer', 'worker', 'secret-session', 'private-model', 'private-title'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(bundle['coverage']['rolled_up_requests'], 1)
        self.assertEqual(bundle['coverage']['derived_requests'], 1)

    def test_snapshot_window_and_scenario_are_explicit(self):
        rows = [row('2026-10-09T23:00:00Z'), row('2026-10-10T00:30:00Z'),
                row('2026-10-10T01:30:00Z')]
        rows[0]['id_synthetic'] = True
        bundle = facts(rows, start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T01:00:00Z', context_threshold=1)
        self.assertEqual(bundle['totals']['requests'], 1)
        self.assertTrue(bundle['window']['partial'])
        self.assertEqual(bundle['previous_window']['start'], '2026-10-09T22:00:00.000Z')
        self.assertEqual(bundle['previous_coverage']['synthetic_excluded'], 1)
        self.assertFalse(self.get_fact(bundle, 'token_change')['values']['previous']['complete'])
        self.assertFalse(self.get_fact(bundle, 'token_change')['complete'])
        self.assertEqual(self.get_fact(bundle, 'token_change')['values']['comparison'], 'equal_elapsed_windows')
        estimate = scenario(bundle, 'large_context_input', 25)
        self.assertEqual(estimate['hypothetical_reduction'], 8.75)
        self.assertEqual(estimate['semantics'], 'hypothetical_input_reduction_not_savings')
        with self.assertRaises(ValueError):
            scenario(bundle, 'tokens', 25)

    def test_change_with_complete_counters_but_partial_period_is_not_complete(self):
        bundle = facts([row('2026-10-09T23:00:00Z'), row('2026-10-10T00:30:00Z')],
                       start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T01:00:00Z')
        self.assertTrue(bundle['totals']['complete'])
        self.assertTrue(bundle['window']['partial'])
        self.assertFalse(self.get_fact(bundle, 'token_change')['complete'])

    def test_invalid_ranges_and_numeric_settings_rejected(self):
        args = dict(start='2026-10-10T00:00:00Z', end='2026-10-10T01:00:00Z',
                    snapshot='2026-10-10T01:00:00Z')
        for overrides in ({'top_n': True}, {'top_n': 0}, {'top_n': 10001}, {'context_threshold': 0},
                          {'context_threshold': -1}, {'contributor_limit': 0},
                          {'contributor_limit': 101}, {'end': args['start']},
                          {'timezone': 'Not/AZone'}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                facts([], **(args | overrides))
        bundle = facts([], **args)
        for bad in (float('nan'), float('inf'), -1, 101, True):
            with self.assertRaises(ValueError):
                scenario(bundle, 'subagent_input', bad)
        safe = facts([row()], **(args | {'filters': {'model': True, 'search': True,
                                                      'session': 'secret-session', 'extra': True}}))
        self.assertEqual(safe['filters'], {'model': True, 'search': True})

    def test_delegation_denominator_includes_disjoint_auto_review(self):
        bundle = facts([row(), row('2026-10-10T01:05:00Z', efficiency_auto_review=True)],
                       start='2026-10-10T00:00:00Z', end='2026-10-10T02:00:00Z',
                       snapshot='2026-10-10T02:00:00Z')
        delegation = self.get_fact(bundle, 'delegation_volume')
        self.assertEqual(delegation['formula'], 'subagent_work_tokens / all_known_tokens')
        self.assertEqual(delegation['denominator'], 84)


if __name__ == '__main__':
    unittest.main()
