"""OpenCode V2 (session_v2 / session_message / session_part) ingestion, key stability and the stale-parser check (TOKEN-186).

Row shapes mirror a real migrated opencode.db (keys only; every value here is synthetic): V2 message data has
no role/parentID/path, carries model{id,providerID,variant}, time{created,completed}, tokens without a total,
content and, on failures, error{type,message}; session_part is empty.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import report, why
from tokenatlas.history import History

START = datetime(1970, 1, 1, tzinfo=timezone.utc)
END = datetime(9999, 1, 1, tzinfo=timezone.utc)
T0 = 1788429600000  # 2026-09-03T10:00:00Z in ms
HOUR = 3600 * 1000

LEGACY_DDL = '''
    CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, directory TEXT NOT NULL,
        title TEXT, version TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL, data TEXT NOT NULL);
'''
V2_DDL = '''
    CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, slug TEXT NOT NULL,
        directory TEXT NOT NULL, title TEXT, version TEXT NOT NULL, tokens_input INTEGER DEFAULT 0 NOT NULL,
        time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
        time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE session_part (message_id TEXT NOT NULL, session_id TEXT NOT NULL, ordinal INTEGER NOT NULL, type TEXT NOT NULL,
        call_id TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (message_id, ordinal));
'''


def tokens(i=10, o=5, r=3, cr=20, cw=2):
    return {'input': i, 'output': o, 'reasoning': r, 'cache': {'read': cr, 'write': cw}}


def legacy_data(created, completed=None, *, provider='openai', model='gpt-x', tok=None, parent='user-1', error=None):
    data = {'role': 'assistant', 'parentID': parent, 'providerID': provider, 'modelID': model, 'mode': 'build', 'agent': 'build',
            'path': {'cwd': '/work/app', 'root': '/work/app'}, 'time': {'created': created}, 'tokens': dict(tok or tokens(), total=1)}
    if completed is not None:
        data['time']['completed'] = completed
    if error:
        data['error'] = error
    return data


def v2_data(created, completed=None, *, provider='openai', model='gpt-x', tok=None, error=None, variant='fast'):
    data = {'agent': 'build', 'content': [], 'cost': 0, 'model': {'id': model, 'providerID': provider, 'variant': variant},
            'time': {'created': created}}
    if tok is not False:
        data['tokens'] = tok or tokens()
    if completed is not None:
        data['time']['completed'] = completed
    if error:
        data['error'] = error
    return data


class Db:
    def __init__(self, path, legacy=True, v2=True):
        self.path, self.has_legacy, self.has_v2 = path, legacy, v2
        self.c = sqlite3.connect(path)
        if legacy:
            self.c.executescript(LEGACY_DDL)
        if v2:
            self.c.executescript(V2_DDL)
        self.seq = 0

    def session(self, sid='s1', parent=None):
        legacy, v2 = self.has_legacy, self.has_v2
        if legacy:
            self.c.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?,?)', (sid, 'p', parent, '/work/app', 't', '1.2', T0, T0))
        if v2:
            self.c.execute('INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?)', (sid, 'p', parent, sid, '/work/app', 't', '2.0', 0, T0, T0))

    def legacy(self, mid, data, sid='s1', created=None):
        created = created or data['time']['created']
        self.c.execute('INSERT INTO message VALUES (?,?,?,?,?)', (mid, sid, created, created, json.dumps(data)))

    def v2(self, mid, data, sid='s1', kind='assistant', created=None):
        self.seq += 1
        created = created or data['time']['created']
        self.c.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', (mid, sid, kind, self.seq, created, created, json.dumps(data)))

    def done(self):
        self.c.commit()
        self.c.close()
        return self.path


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collect(self, path, **kw):
        diagnostics = {}
        return why.collect_opencode(path, START, END, diagnostics=diagnostics, **kw), diagnostics

    def test_v2_only_database_is_read_from_the_real_shape(self):
        db = Db(self.root / 'v2.db', legacy=False)
        db.session('s1')
        db.session('child', parent='s1')
        db.v2('m1', v2_data(T0, T0 + 900), 's1')
        db.v2('m2', v2_data(T0 + 1000, T0 + 1500, provider='anthropic', model='opus', variant='high'), 'child')
        db.v2('u1', {'text': 'synthetic', 'time': {'created': T0}}, 's1', kind='user')
        records, diagnostics = self.collect(db.done())
        by_id = {r.call_id: r for r in records}
        self.assertEqual(set(by_id), {'m1', 'm2'})
        m1 = by_id['m1']
        self.assertEqual((m1.provider, m1.model, m1.effort, m1.cwd, m1.thread_kind), ('openai', 'gpt-x', 'fast', '/work/app', 'main'))
        self.assertEqual((m1.fresh_input, m1.cache_read, m1.cache_write, m1.output, m1.reasoning), (10, 20, 2, 8, 3))
        self.assertEqual(m1.timestamp, datetime.fromtimestamp((T0 + 900) / 1000, tz=timezone.utc))
        self.assertEqual((by_id['m2'].thread_kind, by_id['m2'].parent_session_id), ('subagent', 's1'))
        self.assertEqual(diagnostics['schema_errors'], 0)
        self.assertEqual(diagnostics['source_newest_ms'], T0 + 1000)  # the user row carries no tokens

    def test_legacy_row_wins_over_the_v2_copy_of_the_same_id(self):
        db = Db(self.root / 'both.db')
        db.session()
        db.legacy('shared', legacy_data(T0, T0 + 900, provider='openai', model='gpt-x'))
        db.v2('shared', v2_data(T0, T0 + 901, provider='openai', model='gpt-x'))  # real V2 copies differ from legacy by ~1 ms in completed
        db.v2('only-v2', v2_data(T0 + 5000, T0 + 5500))
        records, _ = self.collect(db.done())
        by_id = {r.call_id: r for r in records}
        self.assertEqual(sorted(by_id), ['only-v2', 'shared'])
        shared = by_id['shared']
        self.assertEqual(shared.timestamp, datetime.fromtimestamp((T0 + 900) / 1000, tz=timezone.utc))
        self.assertEqual((shared.turn_id, shared.turn_confidence, shared.cwd), ('user-1', 'observed', '/work/app'))
        self.assertEqual(by_id['only-v2'].turn_confidence, 'absent')

    def test_unusable_legacy_row_does_not_hide_the_v2_copy(self):
        db = Db(self.root / 'fallback.db')
        db.session()
        db.c.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('same', 's1', T0, T0, '{'))
        db.v2('same', v2_data(T0, T0 + 10))
        records, diagnostics = self.collect(db.done())
        self.assertEqual([r.call_id for r in records], ['same'])
        self.assertEqual(diagnostics['malformed_lines'], 1)

    def test_v2_aborted_error_marks_the_request_interrupted(self):
        db = Db(self.root / 'abort.db', legacy=False)
        db.session()
        db.v2('m1', v2_data(T0, T0 + 10, error={'type': 'aborted', 'message': 'x'}))
        records, _ = self.collect(db.done())
        self.assertEqual(len(records), 1)
        self.assertEqual(list(records[0].flags), [why.INTERRUPTED])

    def test_only_id_session_id_and_data_are_required(self):
        path = self.root / 'minimal.db'
        c = sqlite3.connect(path)
        c.executescript('CREATE TABLE session_v2 (id TEXT PRIMARY KEY); CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, data TEXT);')
        c.execute("INSERT INTO session_v2 VALUES ('s1')")
        c.execute('INSERT INTO session_message VALUES (?,?,?,?)', ('m1', 's1', 'assistant', json.dumps(v2_data(T0, T0 + 5))))
        c.commit(); c.close()
        records, diagnostics = self.collect(path)
        self.assertEqual([r.call_id for r in records], ['m1'])
        self.assertEqual(diagnostics['schema_errors'], 0)
        self.assertEqual(diagnostics['checks_skipped'], 1)  # no time_created: the stale check says so

    def test_message_without_session_row_is_read_and_counted_as_orphan(self):
        db = Db(self.root / 'orphan.db', legacy=False)
        db.v2('m1', v2_data(T0, T0 + 5), sid='gone')
        records, diagnostics = self.collect(db.done())
        self.assertEqual(len(records), 1)
        self.assertEqual(diagnostics['orphan_rows'], 1)

    def test_half_present_pair_and_missing_required_columns_are_schema_errors(self):
        for name, ddl, expected in (
            ('half', 'CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);', 'exists without session_v2'),
            ('nodata', 'CREATE TABLE session_v2 (id TEXT); CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT);', 'lack required columns'),
            ('notype', 'CREATE TABLE session_v2 (id TEXT); CREATE TABLE session_message (id TEXT, session_id TEXT, data TEXT);', 'lack required columns'),
        ):
            path = self.root / f'{name}.db'
            c = sqlite3.connect(path); c.executescript(ddl); c.close()
            records, diagnostics = self.collect(path)
            self.assertEqual((records, diagnostics['schema_errors']), ([], 1), name)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'unsupported OpenCode schema.*' + expected):
                why.collect_opencode(path, START, END)

    def test_v2_turn_is_the_nearest_preceding_user_or_synthetic_row(self):
        db = Db(self.root / 'turn.db', legacy=False)
        db.session()
        db.v2('u1', {'text': 'q', 'time': {'created': T0}}, kind='user')
        db.v2('a1', v2_data(T0 + 10, T0 + 20))
        db.v2('syn1', {'text': 'q', 'time': {'created': T0 + 30}}, kind='synthetic')
        db.v2('a2', v2_data(T0 + 40, T0 + 50))
        db.v2('a3', v2_data(T0 + 60, T0 + 70))
        # an abort without usage right after a3 is attributed to the request that was running in that turn
        db.v2('ab', v2_data(T0 + 80, T0 + 90, tok=False, error={'type': 'aborted', 'message': 'x'}))
        records, _ = self.collect(db.done())
        by_id = {r.call_id: r for r in records}
        self.assertEqual({k: (v.turn_id, v.turn_confidence) for k, v in by_id.items()},
                         {'a1': ('u1', 'observed'), 'a2': ('syn1', 'observed'), 'a3': ('syn1', 'observed')})
        self.assertEqual({k: list(v.flags) for k, v in by_id.items() if v.flags}, {'a3': [why.INTERRUPTED]})

    def test_v2_turn_equals_legacy_parent_for_shared_ids(self):
        db = Db(self.root / 'shared-turn.db')
        db.session()
        db.v2('u1', {'text': 'q', 'time': {'created': T0}}, kind='user')
        db.v2('a1', v2_data(T0 + 10, T0 + 20))
        db.v2('syn', {'text': 'q', 'time': {'created': T0 + 30}}, kind='synthetic')
        db.v2('a2', v2_data(T0 + 40, T0 + 50))
        db.legacy('a1', legacy_data(T0 + 10, T0 + 20, parent='u1'))
        db.legacy('a2', legacy_data(T0 + 40, T0 + 50, parent='syn'))
        path = db.done()
        both, _ = self.collect(path)
        c = sqlite3.connect(path); c.executescript('DROP TABLE message; DROP TABLE session;'); c.close()
        v2_only, _ = self.collect(path)
        self.assertEqual({r.call_id: r.turn_id for r in both}, {r.call_id: r.turn_id for r in v2_only})

    def test_shared_no_usage_abort_flags_only_the_legacy_turn(self):
        db = Db(self.root / 'shared-abort.db')
        db.session()
        db.v2('u1', {'text': 'q', 'time': {'created': T0}}, kind='user')
        db.v2('a1', v2_data(T0 + 10, T0 + 20))
        db.v2('u2', {'text': 'q', 'time': {'created': T0 + 30}}, kind='user')
        db.v2('a2', v2_data(T0 + 40, T0 + 50))
        db.legacy('a1', legacy_data(T0 + 10, T0 + 20, parent='u1'))
        db.legacy('a2', legacy_data(T0 + 40, T0 + 50, parent='u2'))
        # The same no-usage abort in both generations, with differing turns: legacy says u1, the V2 seq rule says u2.
        db.legacy('ab', {'role': 'assistant', 'parentID': 'u1', 'time': {'created': T0 + 60, 'completed': T0 + 60}, 'error': {'name': 'MessageAbortedError'}})
        db.v2('ab', v2_data(T0 + 60, T0 + 60, tok=False, error={'type': 'aborted', 'message': 'x'}))
        records, _ = self.collect(db.done())
        self.assertEqual({r.call_id: list(r.flags or []) for r in records}, {'a1': [why.INTERRUPTED], 'a2': []})

    def test_unusable_legacy_abort_timing_lets_the_v2_abort_through(self):
        db = Db(self.root / 'abort-timing.db')
        db.session()
        db.v2('u1', {'text': 'q', 'time': {'created': T0}}, kind='user')
        db.v2('a1', v2_data(T0 + 10, T0 + 20))
        db.legacy('a1', legacy_data(T0 + 10, T0 + 20, parent='u1'))
        # The legacy abort's time is not a number (and wins over the row columns); the V2 copy has a valid one.
        db.legacy('ab', {'role': 'assistant', 'parentID': 'u1', 'time': {'completed': 'garbled'}, 'error': {'name': 'MessageAbortedError'}}, created=T0 + 60)
        db.v2('ab', v2_data(T0 + 60, T0 + 60, tok=False, error={'type': 'aborted', 'message': 'x'}))
        records, _ = self.collect(db.done())
        self.assertEqual({r.call_id: list(r.flags or []) for r in records}, {'a1': [why.INTERRUPTED]})

    def test_one_readable_generation_is_not_discarded_when_the_other_is_broken(self):
        db = Db(self.root / 'mixed.db', legacy=False)
        db.session()
        db.v2('m1', v2_data(T0, T0 + 5))
        db.c.execute('CREATE TABLE message (id TEXT PRIMARY KEY)')  # legacy message without its session table
        records = why.collect_opencode(db.done(), START, END)  # no diagnostics: must not raise, the V2 side is readable
        self.assertEqual([r.call_id for r in records], ['m1'])

    def test_message_like_table_the_stale_check_cannot_read_is_reported(self):
        db = Db(self.root / 'untimed.db', legacy=False)
        db.session()
        db.v2('m1', v2_data(T0, T0 + 5))
        db.c.execute('CREATE TABLE "we""ird_message" (id TEXT, data TEXT)')
        _, diagnostics = self.collect(db.done())
        self.assertEqual((diagnostics['schema_errors'], diagnostics['checks_skipped']), (0, 1))
        self.assertIn('stale-parser check skipped for we"ird_message', diagnostics['notes'][0])

    def test_no_known_table_still_raises(self):
        path = self.root / 'none.db'
        sqlite3.connect(path).close()
        with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
            why.collect_opencode(path, START, END)

    def test_newest_usage_ignores_in_flight_and_zero_token_rows(self):
        db = Db(self.root / 'newest.db', legacy=False)
        db.session()
        db.v2('done', v2_data(T0, T0 + 10))
        db.v2('zero', v2_data(T0 + 2 * HOUR, T0 + 2 * HOUR, tok=tokens(0, 0, 0, 0, 0)))
        db.v2('flight', v2_data(T0 + 3 * HOUR, tok=False))
        _, diagnostics = self.collect(db.done())
        self.assertEqual(diagnostics['source_newest_ms'], T0)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hist = self.root / 'state/history.sqlite3'

    def refresh(self, path):
        with History(self.hist) as h:
            result = h.refresh('opencode', path)
            return result, h.records(), h.doctor()

    def test_migration_adds_v2_only_rows_without_touching_existing_observations(self):
        path = self.root / 'oc.db'
        db = Db(path, v2=False)  # not migrated yet
        db.session()
        for i in range(3):
            db.legacy(f'm{i}', legacy_data(T0 + i * 1000, T0 + i * 1000 + 500, parent=f'u{i}'))
        db.done()
        before_result, before, _ = self.refresh(path)
        self.assertEqual((before_result['status'], len(before)), ('ok', 3))
        snapshot = {r['id']: r for r in before}  # whole observation, not a few fields

        c = sqlite3.connect(path)
        c.executescript(V2_DDL)  # OpenCode migrates: V2 tables appear next to the legacy ones
        c.execute('INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?)', ('s1', 'p', None, 's1', '/work/app', 't', '2.0', 0, T0, T0))
        for i in range(3):  # V2 copies of the same ids, ~1 ms off in completed like the real database
            c.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', (f'm{i}', 's1', 'assistant', i + 1, T0 + i * 1000, T0 + i * 1000,
                                                                              json.dumps(v2_data(T0 + i * 1000, T0 + i * 1000 + 501))))
        c.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', ('new1', 's1', 'assistant', 9, T0 + 9000, T0 + 9000,
                                                                          json.dumps(v2_data(T0 + 9000, T0 + 9500))))
        c.commit(); c.close()
        after_result, after, doctor = self.refresh(path)
        self.assertEqual(after_result['status'], 'ok')
        self.assertEqual(len(after), 4)
        old = {r['id']: r for r in after if r['id'] in snapshot}
        self.assertEqual(old, snapshot)
        total = lambda rows: sum(sum(v or 0 for v in r['tokens'].values() if isinstance(v, int)) for r in rows)
        self.assertEqual(total(after) - total(before), total([r for r in after if r['id'] == 'new1']))
        self.assertGreater(total([r for r in after if r['id'] == 'new1']), 0)

    def _stale_case(self, build):
        path = self.root / 'case.db'
        db = Db(path)
        db.session()
        build(db)
        return self.refresh(db.done())

    def test_unknown_newer_message_table_marks_the_import_stale_and_not_ok(self):
        def build(db):
            db.legacy('old', legacy_data(T0, T0 + 5))
            db.c.execute('CREATE TABLE message_v3 (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)')
            db.c.execute('INSERT INTO message_v3 VALUES (?,?,?,?)', ('x', 's1', T0 + 5 * HOUR, json.dumps({'tokens': tokens()})))
        result, records, doctor = self._stale_case(build)
        self.assertEqual((result['status'], result['stale_parser'], len(records)), ('partial', 1, 1))
        self.assertTrue(any('stale parser: newest source usage row is 2026-09-03T15' in e for e in result['errors']), result['errors'])
        self.assertIsNone(result['last_success'])
        with History(self.hist) as h:  # the fingerprint-skip path keeps the diagnostics and their text
            again = h.refresh('opencode', self.root / 'case.db')
        self.assertEqual((again['files_skipped'], again['stale_parser']), (1, 1))
        self.assertEqual(again['errors'], result['errors'])
        entry = doctor['imports'][0]
        self.assertEqual((entry['status'], entry['stale_parser']), ('partial', 1))
        self.assertEqual(doctor['files_by_harness']['opencode']['stale_parser'], 1)
        key = report.coverage_key(doctor)
        self.assertEqual(key['imports'][0]['stale_parser'], 1)
        self.assertEqual(key['imports'][0]['status'], 'partial')

    def test_usage_row_the_parser_does_not_recognise_marks_the_import_stale(self):
        def build(db):
            db.legacy('old', legacy_data(T0, T0 + 5))
            db.v2('renamed', v2_data(T0 + 5 * HOUR, T0 + 5 * HOUR + 5), kind='agent-turn')  # an unknown message type with usage: the reader skips it
        result, records, _ = self._stale_case(build)
        self.assertEqual((result['status'], result['stale_parser'], len(records)), ('partial', 1, 1))

    def test_in_flight_zero_token_and_orphan_rows_are_not_stale(self):
        def build(db):
            db.legacy('old', legacy_data(T0, T0 + 5))
            db.v2('zero', v2_data(T0 + 5 * HOUR, T0 + 5 * HOUR, tok=tokens(0, 0, 0, 0, 0)))
            db.v2('flight', v2_data(T0 + 6 * HOUR, tok=False))
            db.v2('autherr', v2_data(T0 + 6 * HOUR, T0 + 6 * HOUR + 3, tok=False, error={'type': 'provider.auth', 'message': 'x'}))
            db.v2('orphan', v2_data(T0 + 7 * HOUR, T0 + 7 * HOUR + 1), sid='gone')  # imported through the LEFT JOIN
            db.v2('user', {'text': 'q', 'time': {'created': T0 + 8 * HOUR}}, kind='user')
        result, records, _ = self._stale_case(build)
        self.assertEqual((result['status'], result['stale_parser'], result['orphan_rows']), ('ok', 0, 1))
        self.assertEqual(len(records), 2)

    def test_grace_period_tolerates_a_slightly_newer_source_row(self):
        def build(db):
            db.legacy('old', legacy_data(T0, T0 + 5))
            db.c.execute('CREATE TABLE message_v3 (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)')
            db.c.execute('INSERT INTO message_v3 VALUES (?,?,?,?)', ('x', 's1', T0 + HOUR // 2, json.dumps({'tokens': tokens()})))
        result, _, _ = self._stale_case(build)
        self.assertEqual(result['stale_parser'], 0)

    def test_schema_error_is_reported_partial_but_checkpointed(self):
        path = self.root / 'half.db'
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)')
        c.commit(); c.close()
        first, _, _ = self.refresh(path)
        second, _, doctor = self.refresh(path)
        self.assertEqual((first['status'], first['schema_errors'], first['files_skipped']), ('partial', 1, 0))
        self.assertIn('session_message exists without session_v2', first['errors'][0])
        self.assertEqual((second['status'], second['schema_errors'], second['files_skipped']), ('partial', 1, 1))
        self.assertEqual(doctor['files_by_harness']['opencode']['schema_errors'], 1)

    def test_report_template_counts_the_new_diagnostics(self):
        template = (Path(__file__).parent / 'tokenatlas/report_template.html').read_text()
        self.assertEqual(template.count('stale_parser||0'), 2)
        self.assertEqual(template.count('schema_errors||0'), 2)


if __name__ == '__main__':
    unittest.main()
