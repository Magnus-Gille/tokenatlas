import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tokenatlas import why
from tokenatlas.__main__ import main
from tokenatlas.history import History
from test_history import _v1_database, write_claude


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CoworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state/history.sqlite3'
        self.main_root = self.root / 'claude/projects'
        self.base = self.root / 'cowork'
        for patcher in (patch.object(why, 'CLAUDE_PROJECTS', self.main_root),
                        patch.object(why, 'COWORK_SESSIONS', self.base)):
            patcher.start(); self.addCleanup(patcher.stop)

    def session(self, org='o', acct='a', name='local_1', request='cw1'):
        directory = self.base / org / acct / name
        write_claude(directory / '.claude/projects/-work/s.jsonl', request)
        # The SDK stream copy of the same call under different id keys must never be imported.
        (directory / 'audit.jsonl').write_text(json.dumps({'type': 'assistant', 'request_id': 'req_x',
            'timestamp': '2026-09-03T10:00:00Z', 'message': {'id': 'msg_x', 'model': 'test-model',
            'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20, 'output_tokens': 5}}}) + '\n')
        return directory

    def test_cowork_roots_are_found_at_fixed_depth(self):
        d = self.session()
        write_claude(self.base / 'o/a/notlocal/.claude/projects/p/x.jsonl', 'nope')
        write_claude(self.base / 'o/local_shallow/.claude/projects/p/x.jsonl', 'nope2')
        self.assertEqual(why.cowork_roots(), [d / '.claude/projects'])

    def test_cowork_transcripts_imported_once_and_audit_ignored(self):
        self.session(); self.session(name='local_2', request='cw2')
        write_claude(self.main_root / 'p/m.jsonl', 'main1')
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude')
        self.assertEqual(code, 0, out)
        with History(self.db) as h:
            rows = h.records()
            self.assertEqual(sorted(r['id'] for r in rows), ['cw1', 'cw2', 'main1'])
            self.assertFalse([r for r in rows if 'audit' in ' '.join(r['sources'])])
            self.assertEqual({r['id']: r['origin'] for r in rows},
                             {'cw1': 'local-agent', 'cw2': 'local-agent', 'main1': 'unknown'})
            self.assertEqual(len(h.connection.execute('SELECT * FROM imports').fetchall()), 3)

    def test_explicit_entrypoint_is_kept_under_cowork(self):
        d = self.session()
        path = d / '.claude/projects/-work/s.jsonl'
        rows = [json.loads(x) for x in path.read_text().splitlines()]
        rows[-1]['entrypoint'] = 'sdk-cli'
        path.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        run('--db', str(self.db), 'refresh', '--harness', 'claude')
        with History(self.db) as h:
            self.assertEqual([r['origin'] for r in h.records()], ['sdk-cli'])

    def test_multi_root_aggregates_and_exit_code(self):
        self.session(); write_claude(self.main_root / 'p/m.jsonl', 'main1')
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude')
        result = json.loads(out)
        self.assertEqual((code, result['status'], len(result['roots'])), (0, 'ok', 2))
        self.assertEqual(result['files_seen'], 2)
        self.assertEqual(result['observations_seen'], 2)
        self.assertEqual([r['root'] for r in result['roots']],
                         [str(self.main_root), str(self.base / 'o/a/local_1/.claude/projects')])
        # A partial Cowork transcript makes the aggregate partial and the exit code non-zero.
        bad = self.base / 'o/a/local_1/.claude/projects/-work/s.jsonl'
        bad.write_text(bad.read_text() + '{"broken\n')
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude')
        result = json.loads(out)
        self.assertEqual((code, result['status']), (2, 'partial'))
        self.assertEqual([r['status'] for r in result['roots']], ['ok', 'partial'])
        self.assertEqual(result['malformed_lines'], 1)

    def test_missing_main_root_is_worst_status(self):
        self.session()
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude')
        result = json.loads(out)
        self.assertEqual((code, result['status']), (2, 'missing'))
        self.assertEqual([r['status'] for r in result['roots']], ['missing', 'ok'])

    def test_absent_cowork_base_is_skipped_and_single_root_output_unchanged(self):
        write_claude(self.main_root / 'p/m.jsonl', 'main1')
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude')
        result = json.loads(out)
        self.assertEqual((code, result['status'], 'roots' in result), (0, 'ok', False))
        with History(self.db) as h:
            self.assertEqual(len(h.connection.execute('SELECT * FROM imports').fetchall()), 1)

    def test_explicit_root_keeps_single_root_behavior(self):
        d = self.session(); write_claude(self.main_root / 'p/m.jsonl', 'main1')
        code, out, _ = run('--db', str(self.db), 'refresh', '--harness', 'claude',
                           '--root', str(self.main_root))
        result = json.loads(out)
        self.assertEqual((code, 'roots' in result, result['observations_seen']), (0, False, 1))


class SnapshotImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local, self.remote = self.root / 'local.sqlite3', self.root / 'pi/history.sqlite3'
        write_claude(self.root / 'l/l.jsonl', 'shared')
        write_claude(self.root / 'l/only.jsonl', 'local-only')
        write_claude(self.root / 'r/r.jsonl', 'shared', output=9)
        write_claude(self.root / 'r/r2.jsonl', 'remote-only')
        with History(self.local) as h:
            h.refresh('claude', self.root / 'l'); self.local_machine = h.machine
        with History(self.remote) as h:
            h.refresh('claude', self.root / 'r'); self.remote_machine = h.machine
        self.snap = self.root / 'pi.snapshot'

    def snapshot(self, db=None, out=None):
        code, out_text, err = run('--db', str(db or self.remote), 'snapshot', str(out or self.snap))
        self.assertEqual(code, 0, err)
        return out_text

    def state(self):
        with History(self.local) as h:
            c = h.connection
            return (h.records(), c.execute('SELECT harness,path,root,fingerprint FROM files ORDER BY path').fetchall() and
                    [tuple(r) for r in c.execute('SELECT harness,path,root,fingerprint FROM files ORDER BY path')],
                    c.execute('SELECT count(*) FROM sources').fetchone()[0])

    def test_snapshot_is_consistent_private_and_atomic(self):
        self.snapshot()
        if os.name != 'nt':  # Windows has no POSIX permission bits
            self.assertEqual(stat.S_IMODE(self.snap.stat().st_mode), 0o600)
        with History(self.snap) as h, History(self.remote) as r:
            self.assertEqual(h.records(), r.records()); self.assertEqual(h.machine, r.machine)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.')], [])
        self.snap.chmod(0o644); self.snapshot()  # overwrite in place, mode restored
        if os.name != 'nt':  # Windows has no POSIX permission bits
            self.assertEqual(stat.S_IMODE(self.snap.stat().st_mode), 0o600)

    def test_snapshot_refuses_the_database_itself(self):
        with History(self.remote) as h: before = h.records()
        code, _, err = run('--db', str(self.remote), 'snapshot', str(self.remote))
        self.assertEqual(code, 2); self.assertIn('database', err)
        link = self.root / 'link.sqlite3'; link.symlink_to(self.remote)
        self.assertEqual(run('--db', str(self.remote), 'snapshot', str(link))[0], 2)
        with History(self.remote) as h: self.assertEqual(h.records(), before)
        self.assertEqual([p.name for p in self.remote.parent.iterdir()], ['history.sqlite3'])

    def imp(self, snap=None, label='pi:huginmunin.local'):
        return run('--db', str(self.local), 'import', str(snap or self.snap), '--label', label)

    def test_import_merges_preserves_machine_prefixes_sources_and_is_idempotent(self):
        self.snapshot()
        code, out, err = self.imp()
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual((summary['observations_seen'], summary['new'], summary['merged'],
                          summary['source_machine']), (2, 1, 1, self.remote_machine))
        with History(self.local) as h:
            rows = {r['id']: r for r in h.records()}
            self.assertEqual(sorted(rows), ['local-only', 'remote-only', 'shared'])
            self.assertEqual(rows['remote-only']['machine'], self.remote_machine)
            self.assertEqual(rows['local-only']['machine'], self.local_machine)
            self.assertEqual(rows['shared']['tokens']['output'], 9)  # max-merge, not double count
            self.assertEqual(rows['remote-only']['sources'], [f"{self.remote_machine}:{self.root / 'r' / 'r2.jsonl'}"])
            prefixed = f"{self.remote_machine}:{self.root / 'r' / 'r.jsonl'}"
            self.assertIn(prefixed, rows['shared']['sources'])
            files = h.connection.execute('SELECT root,fingerprint FROM files WHERE path=?', (prefixed,)).fetchone()
            self.assertEqual(tuple(files), (None, None))
            self.assertEqual(json.loads(h.connection.execute("SELECT value FROM meta WHERE key='machine_labels'")
                                        .fetchone()[0]), {self.remote_machine: 'pi:huginmunin.local'})
            self.assertEqual(h.doctor()['missing_source_files'], 0)
            row = h.connection.execute("SELECT data FROM imports WHERE harness='import'").fetchone()
            self.assertEqual(json.loads(row[0])['source_machine'], self.remote_machine)
        before = self.state()
        code, out, _ = self.imp()
        self.assertEqual((code, json.loads(out)['new']), (0, 0))
        self.assertEqual(self.state(), before)

    def test_doctor_counts_missing_local_files_only(self):
        self.snapshot(); self.imp()
        (self.root / 'l/only.jsonl').unlink()
        with History(self.local) as h:
            self.assertEqual(h.doctor()['missing_source_files'], 1)

    def test_self_import_is_a_noop_with_warning(self):
        self.snapshot(self.local)
        before = self.state()
        code, out, err = self.imp()
        self.assertEqual(code, 0); self.assertIn('warning', err.lower())
        self.assertTrue(json.loads(out)['skipped'])
        self.assertEqual(self.state(), before)
        with History(self.local) as h:
            self.assertEqual(h.connection.execute("SELECT count(*) FROM imports WHERE harness='import'").fetchone()[0], 0)
            self.assertIsNone(h.connection.execute("SELECT 1 FROM meta WHERE key='machine_labels'").fetchone())

    def test_v1_snapshot_imports_and_source_stays_byte_identical(self):
        with History(self.remote) as h:
            items = h.records()
        legacy = self.root / 'legacy.sqlite3'
        _v1_database(legacy, items, [])
        legacy.chmod(0o600)
        before = (sha(legacy), legacy.stat().st_mtime_ns)
        code, out, err = self.imp(legacy, 'old')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['source_machine'], 'm-a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1')
        self.assertEqual((sha(legacy), legacy.stat().st_mtime_ns), before)
        with sqlite_ro(legacy) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
        with History(self.local) as h:
            self.assertEqual(sorted(r['id'] for r in h.records()), ['local-only', 'remote-only', 'shared'])
        self.assertEqual([p.name for p in self.root.iterdir() if 'legacy' in p.name], ['legacy.sqlite3'])

    def test_current_snapshot_stays_byte_identical(self):
        self.snapshot(); before = sha(self.snap)
        self.imp(); self.assertEqual(sha(self.snap), before)

    def test_unknown_schema_is_refused(self):
        self.snapshot()
        import sqlite3
        c = sqlite3.connect(str(self.snap)); c.execute('PRAGMA user_version=999'); c.commit(); c.close()
        before = self.state()
        code, _, err = self.imp()
        self.assertEqual(code, 2); self.assertIn('schema', err)
        self.assertEqual(self.state(), before)

    def test_non_history_file_is_refused(self):
        junk = self.root / 'junk'; junk.write_text('not sqlite')
        self.assertEqual(self.imp(junk)[0], 2)
        empty = self.root / 'empty.sqlite3'
        import sqlite3
        sqlite3.connect(str(empty)).close()
        self.assertEqual(self.imp(empty)[0], 2)


class sqlite_ro:
    def __init__(self, path): self.path = path
    def __enter__(self):
        import sqlite3
        self.c = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True); return self.c
    def __exit__(self, *_): self.c.close()


@unittest.skipIf(os.name == 'nt', 'remote_sync.sh targets macOS/Linux hosts')
class RemoteSyncScriptTests(unittest.TestCase):
    def test_script_syntax_and_history_step(self):
        script = Path(__file__).with_name('tokenatlas') / 'remote_sync.sh'
        self.assertEqual(subprocess.run(['bash', '-n', str(script)]).returncode, 0)
        text = script.read_text()
        for needle in ('command -v tokenatlas', 'snapshot', 'history: not installed on', 'import'):
            self.assertIn(needle, text)


if __name__ == '__main__':
    unittest.main()


class DistinctFiles(unittest.TestCase):  # #151
    def test_nested_roots_count_a_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_claude(root / 'logs/proj/a.jsonl', 'r1')
            (root / 'logs/proj/b.jsonl').write_text('{"type":"assistant",broken\n')  # one malformed line
            with History(root / 'h.sqlite3') as h:
                h.refresh('claude', root / 'logs'); h.refresh('claude', root / 'logs/proj')
                status = h.doctor()
            per_root = sum(i['files_seen'] for i in status['imports'] if i['harness'] == 'claude')
            self.assertEqual(per_root, 4)  # the per-root entries overlap
            self.assertEqual(status['files_by_harness']['claude']['files'], 2)
            self.assertEqual(status['files_by_harness']['claude']['malformed_lines'], 1)
            self.assertNotIn(str(root), json.dumps(status['files_by_harness']))  # counts only

    def test_missing_and_imported_files_are_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_claude(root / 'logs/a.jsonl', 'r1'); write_claude(root / 'logs/b.jsonl', 'r2')
            with History(root / 'h.sqlite3') as h:
                h.refresh('claude', root / 'logs')
                (root / 'logs/b.jsonl').unlink()
                status = h.doctor()
            self.assertEqual((status['files_by_harness']['claude']['files'], status['missing_source_files']), (1, 1))
