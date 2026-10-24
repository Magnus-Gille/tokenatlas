import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas.berget_import import import_emissions
from tokenatlas.history import History, _encode, normalize
from tokenatlas.why import AttributionRecord


EMISSIONS = {
    'co2e_grams': 0.04,
    'energy_kwh': 0.0002,
    'operational': {'co2e_grams': 0.03, 'energy_kwh': 0.0002},
    'embodied': {'co2e_grams': 0.01},
    'grid': {'region': 'SE3', 'carbon_intensity_gco2e_per_kwh': 150},
    'methodology': 'berget-co2-calculator',
    'methodology_version': '1.0.0',
}


class BergetImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'history.sqlite3'
        self.jsonl = self.root / 'emissions.jsonl'
        record = AttributionRecord(
            harness='pi', provider='berget', timestamp=datetime(2026, 10, 21, tzinfo=timezone.utc),
            session_id='session', call_id='response-1', model='openai/gpt-oss-120b', effort='unknown',
            project='project', entrypoint='unknown', thread_kind='main', agent='main',
            fresh_input=10, cache_read=0, cache_write=0, output=4, reasoning=0,
            raw_usage={'input': 10, 'cacheRead': 0, 'cacheWrite': 0, 'output': 4, 'reasoning': 0},
        )
        with History(self.db) as history:
            item = normalize(record, history.machine)
            history._insert(history.connection, _encode(item))

    def row(self, *, response_id='response-1', model='openai/gpt-oss-120b', emissions=EMISSIONS):
        return {'harness': 'pi', 'request_id': response_id, 'model': model,
                'timestamp': '2026-10-21T00:00:00Z', 'usage': {'emissions': emissions}}

    def write(self, *rows):
        self.jsonl.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_import_round_trip_and_idempotence(self):
        self.write(self.row())
        with History(self.db) as history:
            first = import_emissions(history, self.jsonl)
            self.assertEqual(first, {'requests': 1, 'updated': 1, 'unchanged': 0})
            revision = history.revision
            self.assertEqual(import_emissions(history, self.jsonl),
                             {'requests': 1, 'updated': 0, 'unchanged': 1})
            self.assertEqual(history.revision, revision)
        with History(self.db) as history:
            environment = history.records()[0]['environment']
            self.assertEqual(environment['co2e_grams'], 0.04)
            self.assertEqual(environment['source'], 'provider_reported_estimate')

    def test_bad_match_rolls_back_all_rows(self):
        self.write(self.row(), self.row(response_id='missing'))
        with History(self.db) as history:
            with self.assertRaisesRegex(ValueError, 'matching Berget observations'):
                import_emissions(history, self.jsonl)
            self.assertIsNone(history.records()[0]['environment'])

    def test_wrong_model_and_invalid_emissions_rejected(self):
        self.write(self.row(model='wrong'))
        with History(self.db) as history:
            with self.assertRaisesRegex(ValueError, 'model or timestamp'):
                import_emissions(history, self.jsonl)
        self.write(self.row(emissions={**EMISSIONS, 'energy_kwh': -1}))
        with History(self.db) as history:
            with self.assertRaisesRegex(ValueError, 'invalid usage.emissions'):
                import_emissions(history, self.jsonl)
        self.write(self.row(emissions={**EMISSIONS, 'co2e_grams': 4}))
        with History(self.db) as history:
            with self.assertRaisesRegex(ValueError, 'invalid usage.emissions'):
                import_emissions(history, self.jsonl)

    def test_cli_receipt_and_report_records(self):
        self.write(self.row())
        command = [sys.executable, '-B', '-m', 'tokenatlas', '--db', str(self.db)]
        imported = subprocess.run([*command, 'import-emissions', str(self.jsonl)],
                                  text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(imported.stdout)['updated'], 1)
        report = subprocess.run([*command, 'report', '--provider', 'berget', '--records'],
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(report.stdout)['records'][0]['environment']['co2e_grams'], 0.04)


if __name__ == '__main__':
    unittest.main()
