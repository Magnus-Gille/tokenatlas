import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenatlas import pricing, prompt_store


class PerformanceCacheTest(unittest.TestCase):
    def test_precomputed_assignments_survive_report_sorting(self):
        from datetime import datetime, timezone
        from tokenatlas import report, prompts
        from test_report import observation
        records = [observation('z', session='sz', turn_id='tz'), observation('a', session='sa', turn_id='ta')]
        self.assertEqual(records[0]['ts'], records[1]['ts'])
        now = datetime(2026,10,8,tzinfo=timezone.utc)
        texts = {(r['harness'],r['session'],r['turn_id']):r['id'] for r in records}
        expected = report.build_report(records, {}, redact=False, now=now, quota=False, prompt_texts=texts)
        actual = report.build_report(records, {}, redact=False, now=now, quota=False, prompt_texts=texts, assigned=prompts.assign_prompts(records))
        self.assertEqual(actual, expected)

    def test_precomputed_assignments_survive_limit_hit_sorting(self):
        from tokenatlas import limits, prompts
        from test_limits import session_rows, write, TABLE
        with tempfile.TemporaryDirectory() as directory:
            source = write(directory, session_rows())
            from tokenatlas.history import History
            with History(Path(directory)/'fixture.sqlite3') as history:
                history.refresh('claude', Path(directory))
                records, events = list(reversed(history.records())), history.limit_events()
            expected = limits.limit_hits(records, events, TABLE)
            self.assertTrue(expected)
            self.assertEqual(limits.limit_hits(records, events, TABLE, assigned=prompts.assign_prompts(records)), expected)

    def test_price_model_cache_rechecks_mutated_aliases(self):
        table = pricing.load_prices()
        model = next((entry for entry in table['models'] if entry.get('aliases')), None)
        self.assertIsNotNone(model)
        alias = model['aliases'][0]
        record = {'provider': model['provider'], 'model': alias, 'harness': 'claude', 'tariff': {},
                  'tokens': {'fresh_input': 1, 'cache_write': 0, 'cache_read': 0, 'output': 1}, 'raw_usage': {}}
        self.assertNotEqual(pricing.price_observation(record, table)['status'], 'unpriced')
        model['aliases'] = []
        try:
            self.assertEqual(pricing.price_observation(record, table)['status'], 'unpriced')
        finally:
            model['aliases'] = [alias]

    def test_selection_token_canonicalizes_nonlexical_rank_order(self):
        from test_prompt_store import rows, fake, fakectx
        from test_pricing import TABLE
        records = rows((1,1000000),(2,3000000),(3,2000000))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/prompt_store.FILE
            prompt_store.update(path, records, TABLE, 'm1', k=2, extract=fake([]), context=fakectx([]),
                                local={source for row in records for source in row['sources']}, revision=7, history_token='tok', prices='prices')
            self.assertTrue(prompt_store.up_to_date(path, 7, 'tok', 'm1', 'prices'))
            for token, machine, prices in [('new','m1','prices'),('tok','m2','prices'),('tok','m1','new')]:
                self.assertFalse(prompt_store.up_to_date(path, 7, token, machine, prices))

    def test_statusline_reuses_only_the_same_database_and_local_day(self):
        from tokenatlas import statusline
        from tokenatlas.history import History
        with tempfile.TemporaryDirectory() as directory, History(Path(directory)/'history.sqlite3') as history:
            statusline.refresh_cache(history)
            path = statusline.cache_path(history.path)
            original = json.loads(path.read_text())
            with patch.object(statusline, 'build_cache', wraps=statusline.build_cache) as build:
                statusline.refresh_cache(history)
                build.assert_not_called()
                tampered = dict(original, source=dict(original['source'], token='different-db'))
                path.write_text(json.dumps(tampered))
                statusline.refresh_cache(history)
                self.assertEqual(build.call_count, 1)
                stale = json.loads(path.read_text()); stale['written_at']='2000-01-01T12:00:00+00:00'
                path.write_text(json.dumps(stale))
                statusline.refresh_cache(history)
                self.assertEqual(build.call_count, 2)

    def test_store_identity_requires_revision_and_selection_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / prompt_store.FILE
            with patch('tokenatlas.prompt_store.prompts.top_prompts', return_value={'prompts': []}):
                prompt_store.update(path, [], pricing.load_prices(), 'm-test', k=3, by='tokens',
                                    assigned=[], ranked={'prompts': []}, revision=7,
                                    history_token='revision-token', prices='table-token')
            self.assertTrue(prompt_store.up_to_date(path, 7, 'revision-token', 'm-test', 'table-token'))
            self.assertFalse(prompt_store.up_to_date(path, 8, 'revision-token', 'm-test', 'table-token'))
            with patch.object(prompt_store, '__version__', 'future-version'):
                self.assertFalse(prompt_store.up_to_date(path, 7, 'revision-token', 'm-test', 'table-token'))
            data = json.loads(path.read_text())
            data['k'] = 4
            path.write_text(json.dumps(data))
            self.assertFalse(prompt_store.up_to_date(path, 7, 'revision-token', 'm-test', 'table-token'))


if __name__ == '__main__':
    unittest.main()
