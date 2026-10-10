import unittest
from datetime import datetime

from tokenatlas.report import build_report


def observation(identity, ts, **changes):
    row = dict(
        id=identity, ts=ts, harness='codex', provider='openai', machine='m',
        project_id='/private/work', project_label='work', session='session',
        parent_session=None, turn_id='turn', turn_confidence='observed',
        model='gpt-test', effort='medium', thread_kind='main', agent='main',
        origin='codex-tui', tokens=dict(fresh_input=10, cache_read=0,
        cache_write=0, output=5, reasoning=0), complete=True,
        id_synthetic=False, warnings=[], flags=[], raw_usage={}, tariff=None,
    )
    row.update(changes)
    return row


class AnalyticsMetadata(unittest.TestCase):
    def test_request_metadata_is_aligned_and_uses_known_pricing_names(self):
        rows = [
            observation('a', '2026-10-01T10:00:00+00:00',
                        tariff={'speed': 'fast', 'service_tier': 'standard'}),
            observation('b', '2026-10-01T10:01:00+00:00',
                        tariff={'speed': 'custom-speed', 'service_tier': 'flex'}),
            observation('c', '2026-10-01T10:02:00+00:00'),
        ]
        # Metadata must follow the sorted report columns, not the caller's order.
        shared = build_report(list(reversed(rows)), {}, now=datetime.fromisoformat('2026-10-02T00:00:00+00:00'))
        meta = shared['analytics_metadata']['request_meta']
        decoded = {
            field: [None if index is None else meta['dict'][field][index]
                    for index in meta['idx'][field]]
            for field in ('speed', 'service_tier')
        }
        self.assertEqual(decoded, {
            'speed': ['fast', None, None],
            'service_tier': ['standard', 'flex', None],
        })

    def test_custom_values_are_private_only_and_never_leak_into_shared_metadata(self):
        rows = [observation('a', '2026-10-01T10:00:00+00:00',
                            tariff={'speed': 'client-speed-secret', 'service_tier': 'priority'})]
        shared = build_report(rows, {}, now=datetime.fromisoformat('2026-10-02T00:00:00+00:00'))
        private = build_report(rows, {}, redact=False,
                               now=datetime.fromisoformat('2026-10-02T00:00:00+00:00'))
        self.assertNotIn('client-speed-secret', str(shared['analytics_metadata']))
        self.assertNotIn('priority', str(shared['analytics_metadata']))
        pmeta = private['analytics_metadata']['request_meta']
        self.assertEqual(pmeta['dict']['speed'], ['client-speed-secret'])
        self.assertEqual(pmeta['dict']['service_tier'], ['priority'])

    def test_turn_start_is_earliest_observation_local_date_proxy(self):
        rows = [
            observation('later', '2026-10-24T23:10:00+00:00'),
            observation('earliest', '2026-10-24T22:30:00+00:00'),
            observation('unassigned', '2026-10-24T22:00:00+00:00', turn_id=None),
        ]
        report = build_report(rows, {}, timezone_name='Europe/Stockholm',
                              now=datetime.fromisoformat('2026-10-26T00:00:00+00:00'))
        starts = report['analytics_metadata']['observed_turn_start_dates']
        self.assertEqual(list(starts.values()), ['2026-10-25'])

    def test_activity_coverage_counts_only_context_included_in_report(self):
        rows = [observation('a', '2026-10-01T10:00:00+00:00')]
        context = {('codex', 'session', 'turn'): {
            'activity': {'shell': 1, 'edits': None, 'web': 0, 'subagents': None},
        }}
        private = build_report(rows, {}, redact=False, prompt_context=context,
                               now=datetime.fromisoformat('2026-10-02T00:00:00+00:00'))
        self.assertEqual(private['analytics_metadata']['activity_coverage'], {
            'source': 'opt_in_top_k_turn_context', 'retained_context_turns': 1,
            'turns_with_activity': 1,
            'dimensions': ['shell', 'edits', 'web', 'subagents'],
            'full_tool_events': False, 'skills': False,
        })
        shared = build_report(rows, {},
                              now=datetime.fromisoformat('2026-10-02T00:00:00+00:00'))
        self.assertEqual(shared['analytics_metadata']['activity_coverage']['retained_context_turns'], 0)
        self.assertEqual(shared['analytics_metadata']['activity_coverage']['turns_with_activity'], 0)


if __name__ == '__main__':
    unittest.main()
