"""Focused regression tests for the SQL candidate filter used by limit_events."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from tokenatlas.history import ALL_FIELDS, History, _decode, _encode, is_limit_event


UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)


def observation(identity, minute, *, quota=None, tokens=None):
    return {
        'id': identity, 'ts': (START + timedelta(minutes=minute)).isoformat(),
        'harness': 'claude', 'provider': 'anthropic', 'harness_version': '1',
        'collector': 'test', 'source_type': 'transcript', 'session': 'session',
        'parent_session': None, 'session_started_at': None, 'thread_kind': 'main',
        'agent': 'main', 'origin': 'cli', 'model': 'model', 'effort': 'standard',
        'project_id': '/work/project', 'project_label': 'project', 'cwd': '/work/project',
        'turn_id': identity, 'turn_confidence': 'observed', 'tariff': None,
        'warnings': [], 'confidence': {field: 'observed' for field in ALL_FIELDS},
        'quota': quota, 'flags': None, 'id_synthetic': False, 'complete': True,
        'output_final': None, 'tokens': tokens or {field: 0 for field in ALL_FIELDS},
        'raw_usage': {}, 'machine': 'm-test', 'sources': [],
    }


class LimitEventQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'history.sqlite3'

    def populate(self, items):
        with History(self.db) as history:
            history.connection.execute('BEGIN')
            for item in items:
                history._insert(history.connection, _encode(item))
            history.connection.commit()

    def test_candidate_filter_matches_full_history_authority(self):
        items = [
            observation('rejected', 1, quota={'status': 'rejected'}),
            observation('event', 2, quota={'status': 'event'}),
            observation('allowed', 3, quota={'status': 'allowed'}),
            observation('malformed', 4, quota='malformed'),
            observation('empty-quota', 5, quota={}),
            observation('no-quota', 6),
            observation('fresh-positive', 7, quota={'status': 'rejected'},
                        tokens={'fresh_input': 1, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'reasoning': 0}),
            observation('cache-positive', 8, quota={'status': 'event'},
                        tokens={'fresh_input': 0, 'cache_read': 2, 'cache_write': 0, 'output': 0, 'reasoning': 0}),
            observation('write-positive', 9, quota={'status': 'event'},
                        tokens={'fresh_input': 0, 'cache_read': 0, 'cache_write': 3, 'output': 0, 'reasoning': 0}),
            observation('output-positive', 10, quota={'status': 'event'},
                        tokens={'fresh_input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 4, 'reasoning': 0}),
            observation('reasoning-positive', 11, quota={'status': 'event'},
                        tokens={'fresh_input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'reasoning': 5}),
        ]
        self.populate(items)
        with History(self.db) as history:
            full = history.records(include_limit_events=True)
            expected = [row for row in full if is_limit_event(row)]
            self.assertEqual(history.limit_events(), expected)
            self.assertEqual([row['id'] for row in expected], ['rejected', 'event'])

    def test_only_zero_or_null_quota_candidates_are_decoded(self):
        items = [
            observation('rejected', 1, quota={'status': 'rejected'}),
            observation('event', 2, quota={'status': 'event'},
                        tokens={'fresh_input': None, 'cache_read': None, 'cache_write': None, 'output': None, 'reasoning': None}),
            observation('allowed', 3, quota={'status': 'allowed'}),
            observation('malformed', 4, quota='malformed'),
            observation('no-quota', 5),
            observation('ordinary', 6, quota={'status': 'allowed'},
                        tokens={'fresh_input': 1, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'reasoning': 0}),
            observation('positive-with-quota', 7, quota={'status': 'rejected'},
                        tokens={'fresh_input': 1, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'reasoning': 0}),
        ]
        self.populate(items)
        with History(self.db) as history:
            with mock.patch('tokenatlas.history._decode', wraps=_decode) as decode:
                found = history.limit_events()
            self.assertEqual([row['id'] for row in found], ['rejected', 'event'])
            # Four rows have a quota reference and all counters are zero/NULL;
            # the no-quota row and both positive-token rows (each with a quota)
            # never reach Python's final authority check.
            self.assertEqual(decode.call_count, 4)

    def test_start_and_end_keep_records_filter_semantics(self):
        self.populate([
            observation('before', 1, quota={'status': 'rejected'}),
            observation('inside', 2, quota={'status': 'event'}),
            observation('after', 3, quota={'status': 'rejected'}),
        ])
        with History(self.db) as history:
            found = history.limit_events(START + timedelta(minutes=2), START + timedelta(minutes=3))
            self.assertEqual([row['id'] for row in found], ['inside'])

    def test_source_paths_match_full_records(self):
        items = [
            observation('first', 1, quota={'status': 'rejected'}),
            observation('second', 2, quota={'status': 'event'}),
        ]
        self.populate(items)
        with History(self.db) as history:
            first_id = history.connection.execute(
                'SELECT id FROM observations WHERE call_id=(SELECT id FROM strings WHERE value=?)',
                ('first',)).fetchone()[0]
            first_file = history._file_id(history.connection, 'claude', '/logs/first.jsonl')
            second_file = history._file_id(history.connection, 'claude', '/logs/second.jsonl')
            history.connection.executemany('INSERT INTO sources VALUES (?,?)', [
                (first_id, first_file), (first_id, second_file)])
            history.connection.commit()
            expected = [row for row in history.records(include_limit_events=True) if is_limit_event(row)]
            found = history.limit_events()
            self.assertEqual(found, expected)
            self.assertEqual(found[0]['sources'], ['/logs/first.jsonl', '/logs/second.jsonl'])
            self.assertEqual(found[1]['sources'], [])


if __name__ == '__main__':
    unittest.main()
