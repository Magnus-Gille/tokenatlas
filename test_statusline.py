import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tokenatlas import statusline
from tokenatlas.__main__ import main, refresh_all
from tokenatlas.history import History

ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)  # 12:00 in Europe/Stockholm
USAGE = {'input_tokens': 1000, 'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 100, 'output_tokens': 500}
OPUS_MWH = 1000 / 1000 * 390 + 2000 / 1000 * 15 + 100 / 1000 * 490 + 500 / 1000 * 1400  # 1169 mWh at multiplier 1


def assistant(request, stamp, model, usage=USAGE, ambiguous=False):
    row = {'type': 'assistant', 'uuid': 'row-' + request, 'requestId': request, 'sessionId': 'session', 'cwd': '/work/app',
           'version': 'test-version', 'timestamp': stamp,
           'message': {'id': 'msg-' + request, 'model': model, 'stop_reason': 'end_turn', 'usage': usage}}
    if ambiguous:
        row.pop('requestId')
        row['message'].pop('id')
    return row


def write_claude(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


ROWS = (assistant('a1', '2026-09-30T10:00:00Z', 'claude-opus-4-8'),  # Sep 30 local
        assistant('a2', '2026-09-30T23:30:00Z', 'claude-sonnet-5'),  # Oct 1 01:30 local: the next local day
        assistant('a3', '2026-10-01T08:00:00Z', 'mystery-model'),  # unweighted
        assistant('a4', '2026-10-01T08:30:00Z', 'claude-opus-4-8', {}),  # no counters: incomplete
        assistant('a5', '2026-10-01T09:00:00Z', 'claude-opus-4-8', ambiguous=True),  # ambiguous identity: excluded
        assistant('a6', '2026-08-01T10:00:00Z', 'claude-opus-4-8'))  # outside the 31 days


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'state' / 'history.sqlite3'
        self.logs = self.root / 'logs'
        write_claude(self.logs / 'a.jsonl', ROWS)
        if hasattr(time, 'tzset'):  # bucketing uses the machine's local zone: pin it
            previous = os.environ.get('TZ')
            os.environ['TZ'] = 'Europe/Stockholm'
            time.tzset()
            self.addCleanup(self.restore_zone, previous)

    @staticmethod
    def restore_zone(previous):
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--db', str(self.db), *args])
        return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class CacheTests(Base):
    def build(self):
        with History(self.db) as h:
            h.refresh('claude', self.logs)
            return statusline.build_cache(h, NOW), h.revision

    def test_buckets_by_local_day_and_counts_each_class(self):
        cache, revision = self.build()
        self.assertEqual(sorted(cache['days']), ['2026-09-30', '2026-10-01'])  # the August row is outside 31 days
        self.assertEqual(cache['revision'], revision)
        self.assertEqual(cache['written_at'], '2026-10-01T10:00:00+00:00')
        sep30, oct1 = cache['days']['2026-09-30'], cache['days']['2026-10-01']
        self.assertEqual({k: sep30[k] for k in statusline.CLASSES}, dict(fresh_input=1000, cache_read=2000, cache_write=100, output=500))
        self.assertEqual((sep30['requests'], sep30['unweighted'], sep30['incomplete']), (1, 0, 0))
        # a2 (sonnet, 23:30Z = 01:30 local), a3 (unknown model), a4 (no counters): ambiguous a5 is not here
        self.assertEqual({k: oct1[k] for k in statusline.CLASSES}, dict(fresh_input=2000, cache_read=4000, cache_write=200, output=1000))
        self.assertEqual((oct1['requests'], oct1['unweighted'], oct1['incomplete']), (3, 1, 1))

    def test_energy_uses_the_model_multiplier(self):
        cache, _ = self.build()
        self.assertAlmostEqual(cache['days']['2026-09-30']['mwh'], OPUS_MWH)
        self.assertAlmostEqual(cache['days']['2026-10-01']['mwh'], OPUS_MWH * 0.6 + OPUS_MWH * 1.0)

    def test_the_fixture_has_an_ambiguous_row(self):
        with History(self.db) as h:
            h.refresh('claude', self.logs)
            self.assertEqual(sum(r['id_synthetic'] for r in h.records()), 1)

    def test_write_is_private_atomic_and_leaves_no_temp_file(self):
        path = self.root / 'out' / statusline.CACHE_NAME
        path.parent.mkdir()
        statusline.write_atomic(path, {'v': 1})
        self.assertEqual(json.loads(path.read_text()), {'v': 1})
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with mock.patch('os.replace', side_effect=OSError('boom')), self.assertRaises(OSError):
            statusline.write_atomic(path, {'v': 2})
        self.assertEqual(json.loads(path.read_text()), {'v': 1})  # the old file survives a failed write
        self.assertEqual([p.name for p in path.parent.iterdir()], [statusline.CACHE_NAME])


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class RefreshTests(Base):
    def test_refresh_writes_the_cache_next_to_the_database(self):
        code, _, _ = self.cli('refresh', '--harness', 'claude', '--root', str(self.logs))
        self.assertEqual(code, 0)
        cache = json.loads((self.db.parent / statusline.CACHE_NAME).read_text())
        self.assertGreaterEqual(cache['revision'], 1)
        self.assertIn('days', cache)

    def test_refresh_all_writes_the_cache(self):
        with mock.patch('tokenatlas.why.harness_root', return_value=(self.root / 'absent', 'test')), \
                mock.patch('tokenatlas.why.cowork_scan', return_value=([], [])), History(self.db) as h:
            refresh_all(h)
        self.assertTrue((self.db.parent / statusline.CACHE_NAME).is_file())

    def test_a_cache_failure_warns_and_does_not_fail_refresh(self):
        with mock.patch.object(statusline, 'build_cache', side_effect=RuntimeError('boom')):
            code, out, err = self.cli('refresh', '--harness', 'claude', '--root', str(self.logs))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'ok')
        self.assertIn('warning: could not write statusline.json: RuntimeError: boom', err)
        self.assertFalse((self.db.parent / statusline.CACHE_NAME).exists())


CACHE = {'v': 1, 'written_at': '2026-10-01T09:50:00+00:00', 'revision': 3, 'days': {
    '2026-10-01': dict(fresh_input=1000, cache_read=1_000_000, cache_write=0, output=1000, mwh=2000.0, requests=2, unweighted=0, incomplete=0),
    '2026-09-28': dict(fresh_input=0, cache_read=0, cache_write=0, output=5_000_000, mwh=7_000_000.0, requests=9, unweighted=0, incomplete=0),
    '2026-09-10': dict(fresh_input=0, cache_read=0, cache_write=0, output=2_000_000_000, mwh=3_000_000_000.0, requests=9, unweighted=0, incomplete=0)}}
PAYLOAD = {'model': {'display_name': 'Opus 4.8'}, 'context_window': {'used_percentage': 42.4},
           'rate_limits': {'five_hour': {'used_percentage': 29.2}, 'seven_day': {'used_percentage': 51.6}}}


@unittest.skipUnless(hasattr(time, 'tzset'), 'needs tzset to pin the local zone')
class StatuslineTests(Base):
    def line(self, payload=PAYLOAD, cache=CACHE, now=NOW, raw=None):
        db = self.db
        if cache is not None:
            db.parent.mkdir(parents=True, exist_ok=True)
            (db.parent / statusline.CACHE_NAME).write_text(json.dumps(cache))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = statusline.run([], db, io.StringIO(json.dumps(payload) if raw is None else raw), now)
        self.assertEqual(code, 0)
        return out.getvalue().rstrip('\n')

    def test_full_payload(self):
        self.assertEqual(self.line(), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52% | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_month_window_is_thirty_days_and_week_is_seven(self):
        # 2026-09-10 is 21 days back (in the month, not the week); 2026-08-31 is 31 days back (in neither)
        old = dict(fresh_input=0, cache_read=0, cache_write=0, output=1_000_000_000, mwh=1e9, requests=1, unweighted=0, incomplete=0)
        cache = dict(CACHE, days=dict(CACHE['days'], **{'2026-08-31': old}))
        self.assertEqual(self.line(cache=cache), self.line())
        self.assertIn('| W:6.0M ~5 kWh | M:2.0B ~2 MWh', self.line())

    def test_without_rate_limits(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != 'rate_limits'}
        self.assertEqual(self.line(payload), 'Opus 4.8 | Ctx:42% | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_unknown_model_and_minimal_payload(self):
        self.assertEqual(self.line({}), '? | D:1.0M ~2 Wh | W:6.0M ~5 kWh | M:2.0B ~2 MWh')

    def test_missing_cache_omits_the_totals(self):
        self.assertEqual(self.line(cache=None), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52%')

    def test_unreadable_cache_omits_the_totals(self):
        self.db.parent.mkdir(parents=True)
        (self.db.parent / statusline.CACHE_NAME).write_text('{not json')
        self.assertEqual(self.line(cache=None), 'Opus 4.8 | Ctx:42% | 5h:29% 7d:52%')

    def test_stale_cache_appends_its_local_time(self):
        stale = dict(CACHE, written_at='2026-10-01T08:40:00+00:00')  # 80 minutes before NOW; 10:40 in Stockholm
        self.assertTrue(self.line(cache=stale).endswith('M:2.0B ~2 MWh (10:40)'))
        fresh = dict(CACHE, written_at='2026-10-01T09:20:00+00:00')  # 40 minutes: still fresh
        self.assertNotIn('(', self.line(cache=fresh))

    def test_midnight_rolls_the_day_total_without_a_rewrite(self):
        line = self.line(now=datetime(2026, 10, 1, 22, 30, tzinfo=timezone.utc))  # 00:30 on Oct 2 locally
        self.assertIn('| D:0 |', line)
        self.assertIn('W:6.0M ~5 kWh', line)

    def test_garbage_stdin_prints_a_fallback_and_exits_zero(self):
        self.assertEqual(self.line(raw='not json'), 'TokenAtlas')
        self.assertEqual(self.line(raw=''), 'TokenAtlas')

    def test_a_bad_value_falls_back_to_the_model_name(self):
        line = self.line({'model': {'display_name': 'Opus 4.8'}, 'context_window': {'used_percentage': 'x'}})
        self.assertTrue(line.startswith('Opus 4.8 | D:'), line)  # a non-numeric value is left out; the rest of the line stays
        self.assertNotIn('Ctx', line)

    def test_it_writes_nothing(self):
        self.line()
        before = sorted(p.name for p in self.db.parent.iterdir())
        mtime = (self.db.parent / statusline.CACHE_NAME).stat().st_mtime_ns
        self.line(cache=None)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before)
        self.assertEqual((self.db.parent / statusline.CACHE_NAME).stat().st_mtime_ns, mtime)

    def test_subprocess_prints_garbage_fallback_and_exits_zero(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(self.db), 'statusline'], input='\x00garbage', text=True,
                              capture_output=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (0, 'TokenAtlas', ''))


class LightImportTests(unittest.TestCase):
    def test_statusline_does_not_import_the_heavy_modules(self):
        code = ('import sys, io\nfrom tokenatlas.__main__ import main\nsys.stdin = io.StringIO("{}")\nmain(["statusline"])\n'
                'heavy = [m for m in ("history", "report", "insights", "pricing", "why", "sessions", "prompt_store") if "tokenatlas." + m in sys.modules]\n'
                'print("HEAVY", heavy)\n')
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT,
                                  env=dict(os.environ, PYTHONPATH=str(ROOT), XDG_STATE_HOME=tmp))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.rstrip().endswith('HEAVY []'), done.stdout)


class RobustnessTests(unittest.TestCase):
    def test_a_bad_option_prints_the_fallback_and_exits_zero(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', 'statusline', '--bogus'], capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT)), input='{}')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'TokenAtlas'))

    def test_help_still_works(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', 'statusline', '--help'], capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(done.returncode, 0)
        self.assertIn('--setup', done.stdout)

    def test_non_finite_percentages_are_left_out(self):
        out = io.StringIO()
        raw = '{"model":{"display_name":"Opus"},"context_window":{"used_percentage":NaN},"rate_limits":{"five_hour":{"used_percentage":Infinity},"seven_day":{"used_percentage":12}}}'
        with contextlib.redirect_stdout(out):
            self.assertEqual(statusline.run([], db=Path(tempfile.gettempdir()) / 'none' / 'h.sqlite3', stdin=io.StringIO(raw)), 0)
        self.assertEqual(out.getvalue().strip(), 'Opus | 7d:12%')


class RecordQuotaTests(Base):
    def payload(self, five=29, seven=52, session='sess-1'):
        return {'model': {'display_name': 'Opus 4.8'}, 'session_id': session,
                'rate_limits': {'five_hour': {'used_percentage': five, 'resets_at': 1790000000}, 'seven_day': {'used_percentage': seven, 'resets_at': 1790500000.0}}}

    def run_line(self, payload, *flags, db=None, now=NOW):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = statusline.run(list(flags), db or self.db, io.StringIO(json.dumps(payload)), now)
        self.assertEqual(code, 0)
        return out.getvalue()

    def lines(self):
        path = self.db.parent / statusline.QUOTA_NAME
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_recording_is_on_by_default(self):
        self.run_line(self.payload())
        self.assertEqual(len(self.lines()), 1)

    def test_the_opt_out_flag_and_env_turn_recording_off(self):
        self.run_line(self.payload(), '--no-record-quota')
        with mock.patch.dict(os.environ, {'TOKENATLAS_NO_QUOTA': '1'}):
            plain = self.run_line(self.payload(five=31))
        self.assertEqual(sorted(p.name for p in self.db.parent.glob('claude-quota*')) if self.db.parent.exists() else [], [])
        with mock.patch.dict(os.environ, {'TOKENATLAS_NO_QUOTA': '0'}):
            self.assertEqual(self.run_line(self.payload(five=32)), plain.replace('31', '32'))
        self.assertEqual(len(self.lines()), 1)

    def test_the_old_flag_still_works_as_a_no_op(self):
        self.run_line(self.payload(), '--record-quota')
        self.assertEqual(len(self.lines()), 1)

    def test_the_flag_appends_a_snapshot_and_keeps_the_output(self):
        plain = self.run_line(self.payload(), '--no-record-quota')
        recorded = self.run_line(self.payload(), '--record-quota')
        self.assertEqual(plain, recorded)
        got = self.lines()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['ts'], '2026-10-01T10:00:00+00:00')
        self.assertEqual(got[0]['session'], 'sess-1')
        self.assertEqual(got[0]['five_hour'], {'used_percent': 29, 'resets_at': datetime.fromtimestamp(1790000000, timezone.utc).isoformat()})
        self.assertEqual(got[0]['seven_day']['used_percent'], 52)

    def test_an_unchanged_value_is_not_written_again_even_from_another_session(self):
        self.run_line(self.payload(), '--record-quota')
        self.run_line(self.payload(session='other'), '--record-quota')
        self.assertEqual(len(self.lines()), 1)
        self.run_line(self.payload(five=30), '--record-quota')
        self.assertEqual([x['five_hour']['used_percent'] for x in self.lines()], [29, 30])

    def test_an_unreadable_last_file_still_appends(self):
        self.run_line(self.payload(), '--record-quota')
        (self.db.parent / statusline.QUOTA_LAST).write_text('not json{')
        self.run_line(self.payload(), '--record-quota')
        self.assertEqual(len(self.lines()), 2)

    def test_a_payload_without_limits_or_one_window_is_handled(self):
        self.run_line({'model': {'display_name': 'X'}}, '--record-quota')
        self.assertEqual(self.lines(), [])
        self.run_line({'session_id': 's', 'rate_limits': {'seven_day': {'used_percentage': 3.5, 'resets_at': 1790500000}, 'five_hour': {'used_percentage': 'x'}}}, '--record-quota')
        got = self.lines()
        self.assertEqual((got[0]['five_hour'], got[0]['seven_day']['used_percent']), (None, 3.5))

    @unittest.skipIf(os.name == 'nt', 'POSIX modes')
    def test_the_files_are_private(self):
        self.run_line(self.payload(), '--record-quota')
        for name in (statusline.QUOTA_NAME, statusline.QUOTA_LAST):
            self.assertEqual((self.db.parent / name).stat().st_mode & 0o777, 0o600)

    def test_a_write_failure_keeps_the_output_identical(self):
        plain = self.run_line(self.payload(), '--no-record-quota')
        blocked = self.root / 'file'
        blocked.write_text('x')  # the "state directory" is a file: nothing can be created in it
        self.assertEqual(self.run_line(self.payload(), '--record-quota', db=blocked / 'history.sqlite3'), plain)
        with mock.patch('os.write', side_effect=OSError('disk full')):
            self.assertEqual(self.run_line(self.payload(), '--record-quota'), plain)

    def test_pruning_keeps_the_last_sixty_days_above_the_size_limit(self):
        path = self.db.parent / statusline.QUOTA_NAME
        path.parent.mkdir(parents=True)
        old = json.dumps({'ts': '2026-07-01T00:00:00+00:00', 'session': 'a', 'five_hour': None, 'seven_day': {'used_percent': 1, 'resets_at': None}})
        new = json.dumps({'ts': '2026-09-20T00:00:00+00:00', 'session': 'a', 'five_hour': None, 'seven_day': {'used_percent': 2, 'resets_at': None}})
        path.write_text('\n'.join([old, new, 'garbage']) + '\n')
        os.chmod(path, 0o600)
        with mock.patch.object(statusline, 'QUOTA_MAX_BYTES', 10):
            self.run_line(self.payload(), '--record-quota')
        got = self.lines()
        self.assertEqual([x['ts'][:10] for x in got], ['2026-09-20', '2026-10-01'])
        self.assertEqual([p.name for p in path.parent.glob('*.tmp')], [])
        # below the limit nothing is rewritten
        path.write_text('\n'.join([old, new]) + '\n')
        os.chmod(path, 0o600)
        self.run_line(self.payload(five=40), '--record-quota')
        self.assertEqual(len(self.lines()), 3)

    def test_the_timestamp_keeps_fractional_seconds(self):
        self.run_line(self.payload(), '--record-quota', now=NOW.replace(microsecond=123456))
        self.assertEqual(self.lines()[0]['ts'], '2026-10-01T10:00:00.123456+00:00')

    @unittest.skipIf(os.name == 'nt', 'uses flock')
    def test_a_held_lock_skips_recording_without_blocking_and_a_free_one_records(self):
        import fcntl
        self.db.parent.mkdir(parents=True)
        plain = self.run_line(self.payload(), '--no-record-quota')
        fd = os.open(self.db.parent / statusline.QUOTA_LOCK, os.O_RDWR | os.O_CREAT, 0o600)  # another process is pruning/appending
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertEqual(self.run_line(self.payload(), '--record-quota'), plain)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(self.lines(), [])
            self.assertFalse((self.db.parent / statusline.QUOTA_LAST).exists())  # the skipped reading is not marked as recorded
        finally:
            os.close(fd)
        self.run_line(self.payload(), '--record-quota')
        self.assertEqual(len(self.lines()), 1)

    def test_prune_cannot_lose_an_append_that_is_in_flight(self):
        """The prune runs inside the lock, so an append that starts during it waits for the next reading instead of being lost to the rewrite."""
        path = self.db.parent / statusline.QUOTA_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'ts': '2026-09-20T00:00:00+00:00', 'session': 'a', 'five_hour': None, 'seven_day': {'used_percent': 2, 'resets_at': None}}) + '\n')
        os.chmod(path, 0o600)
        seen = []
        real = statusline._prune_quota

        def prune_while_another_reading_arrives(p, now):
            seen.append(self.run_line(self.payload(five=77), '--record-quota'))  # a concurrent statusline: the lock is busy, so it skips
            real(p, now)
        with mock.patch.object(statusline, 'QUOTA_MAX_BYTES', 10), mock.patch.object(statusline, '_prune_quota', prune_while_another_reading_arrives):
            self.run_line(self.payload(), '--record-quota')
        self.assertEqual([x['five_hour']['used_percent'] if x['five_hour'] else None for x in self.lines()], [None, 29])
        self.assertEqual(json.loads((self.db.parent / statusline.QUOTA_LAST).read_text())['five_hour']['used_percent'], 29)  # .last matches the last line
        self.run_line(self.payload(five=77), '--record-quota')  # the skipped reading is recorded next time
        self.assertEqual(self.lines()[-1]['five_hour']['used_percent'], 77)

    def test_settings_detection(self):
        cfg = self.root / 'cfg'
        cfg.mkdir()
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(cfg)}):
            self.assertIsNone(statusline.recording_configured())
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'command': 'tokenatlas statusline'}}))
            self.assertIs(statusline.recording_configured(), True)  # on by default
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'command': 'tokenatlas statusline --no-record-quota'}}))
            self.assertIs(statusline.recording_configured(), False)
            with mock.patch.dict(os.environ, {'TOKENATLAS_NO_QUOTA': '1'}):
                (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'command': 'tokenatlas statusline'}}))
                self.assertIs(statusline.recording_configured(), False)
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'command': 'my-own-line'}}))
            self.assertIs(statusline.recording_configured(), False)
            self.assertEqual(statusline.recording_state(), 'foreign')
            (cfg / 'settings.local.json').write_text(json.dumps({'statusLine': {'command': 'tokenatlas statusline --record-quota'}}))
            self.assertIs(statusline.recording_configured(), True)

    def test_the_effective_command_follows_settings_precedence_and_command_local_env(self):
        cfg = self.root / 'cfg2'
        cfg.mkdir()

        def state(base, local=None):
            (cfg / 'settings.json').write_text(json.dumps({'statusLine': {'command': base}}))
            (cfg / 'settings.local.json').write_text(json.dumps({'statusLine': {'command': local}} if local else {}))
            return statusline.recording_state()
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(cfg)}):
            self.assertEqual(state('tokenatlas statusline'), 'enabled')
            self.assertEqual(state('tokenatlas statusline', 'tokenatlas statusline --no-record-quota'), 'disabled')
            self.assertEqual(state('tokenatlas statusline', 'my-own-line'), 'foreign')
            self.assertEqual(state('tokenatlas statusline --no-record-quota', 'tokenatlas statusline'), 'enabled')
            self.assertEqual(state('TOKENATLAS_NO_QUOTA=1 tokenatlas statusline'), 'disabled')
            self.assertEqual(state("FOO=a TOKENATLAS_NO_QUOTA=true '/opt/my bin/tokenatlas' statusline"), 'disabled')
            self.assertEqual(state('TOKENATLAS_NO_QUOTA=0 tokenatlas statusline'), 'enabled')
            with mock.patch.dict(os.environ, {'TOKENATLAS_NO_QUOTA': '1'}):
                self.assertEqual(state('TOKENATLAS_NO_QUOTA=0 tokenatlas statusline'), 'enabled')  # the command's own assignment wins
            self.assertEqual(state('cd /x && TOKENATLAS_NO_QUOTA=1 tokenatlas statusline'), 'unknown')
            self.assertEqual(state("tokenatlas statusline 'unbalanced"), 'unknown')

    @unittest.skipIf(os.name == 'nt', 'POSIX modes and symlinks')
    def test_a_permissive_or_symlinked_snapshot_file_is_not_written_and_doctor_says_so(self):
        path = self.db.parent / statusline.QUOTA_NAME
        path.parent.mkdir(parents=True)
        path.write_text('')
        os.chmod(path, 0o644)
        plain = self.run_line(self.payload(), '--no-record-quota')
        self.assertEqual(self.run_line(self.payload(), '--record-quota'), plain)
        self.assertEqual(path.read_text(), '')
        self.assertEqual(statusline.quota_file_problem(path), 'permissions must be 0600')
        os.chmod(path, 0o600)
        self.assertIsNone(statusline.quota_file_problem(path))
        path.unlink()
        target = self.root / 'elsewhere.jsonl'
        target.write_text('')
        os.symlink(target, path)
        (path.parent / statusline.QUOTA_LAST).unlink(missing_ok=True)
        self.assertEqual(self.run_line(self.payload(), '--record-quota'), plain)
        self.assertEqual(target.read_text(), '')
        self.assertEqual(statusline.quota_file_problem(path), 'must not be a symlink')

    def test_the_default_directory_is_not_created_while_only_the_legacy_one_exists(self):
        state = self.root / 'xdg'
        legacy = state / 'agentmon'
        legacy.mkdir(parents=True)
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(state)}):
            self.assertEqual(statusline.default_db(), legacy / 'history.sqlite3')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                statusline.run(['--record-quota'], None, io.StringIO(json.dumps(self.payload())), NOW)
            self.assertFalse((state / 'tokenatlas').exists())
            self.assertEqual(len((legacy / statusline.QUOTA_NAME).read_text().splitlines()), 1)
            (state / 'tokenatlas').mkdir()
            self.assertEqual(statusline.default_db(), state / 'tokenatlas' / 'history.sqlite3')  # both exist: the new one, as default_db() does
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'fresh')}):
            self.assertEqual(statusline.default_db(), self.root / 'fresh' / 'tokenatlas' / 'history.sqlite3')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFOs')
    def test_a_fifo_in_place_of_the_snapshot_file_never_blocks_the_statusline(self):
        import threading
        self.db.parent.mkdir(parents=True)
        plain = self.run_line(self.payload(), '--no-record-quota')
        for name in (statusline.QUOTA_NAME, statusline.QUOTA_LAST):
            fifo = self.db.parent / name
            os.mkfifo(fifo)
            result = []
            thread = threading.Thread(target=lambda: result.append(self.run_line(self.payload(), '--record-quota')), daemon=True)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive(), f'{name} as a FIFO blocked the statusline')
            self.assertEqual(result, [plain])
            if name == statusline.QUOTA_NAME:
                self.assertTrue(stat.S_ISFIFO(os.stat(fifo).st_mode))  # nothing replaced or written into it
            fifo.unlink(missing_ok=True)

    @unittest.skipUnless(os.path.isdir('/dev/fd'), 'counts open descriptors through /dev/fd')
    def test_no_descriptor_stays_open_after_any_record_path(self):
        """An open handle blocks replacing or deleting the file on Windows, so every path must close what it opens."""
        def open_fds():
            return len(os.listdir('/dev/fd'))
        path = self.db.parent / statusline.QUOTA_NAME
        before = open_fds()
        self.run_line(self.payload(), '--record-quota')                 # create
        self.run_line(self.payload(), '--record-quota')                 # unchanged: skipped
        self.run_line(self.payload(five=31), '--record-quota')          # existing file: validated and appended
        with mock.patch.object(statusline, 'QUOTA_MAX_BYTES', 10):
            self.run_line(self.payload(five=32), '--record-quota')      # prune and atomic replace
        with mock.patch('os.set_blocking', side_effect=OSError('boom')):
            self.run_line(self.payload(five=33), '--record-quota')      # a failure after the open
        with mock.patch('os.write', side_effect=OSError('disk full')):
            self.run_line(self.payload(five=34), '--record-quota')      # a failed write
        os.chmod(path, 0o644)
        self.run_line(self.payload(five=35), '--record-quota')          # rejected as not private
        self.assertEqual(open_fds(), before)

    def test_setup_with_the_flag_prints_it(self):
        text = statusline.setup_text(None, '/opt/bin/tokenatlas')
        self.assertIn('"command": "/opt/bin/tokenatlas statusline"', text)
        self.assertNotIn('statusline --', text)
        self.assertIn('--no-record-quota', text)
        self.assertIn('TOKENATLAS_NO_QUOTA=1', text)


class SetupTests(unittest.TestCase):
    def test_setup_prints_the_snippet_and_never_edits_the_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tmp}), mock.patch.object(statusline, 'executable', return_value='/opt/bin/tokenatlas'), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(statusline.run(['--setup']), 0)
            self.assertEqual(os.listdir(tmp), [])
        text = out.getvalue()
        self.assertIn(str(Path(tmp) / 'settings.json'), text)
        snippet = json.loads(text[text.index('{'):text.rindex('}') + 1])
        self.assertEqual(snippet, {'statusLine': {'type': 'command', 'command': '/opt/bin/tokenatlas statusline'}})

    def test_setup_default_location_and_custom_database(self):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
        with mock.patch.dict(os.environ, env, clear=True):
            text = statusline.setup_text('/data/h.sqlite3', '/opt/my bin/tokenatlas')
        self.assertIn(str(Path.home() / '.claude' / 'settings.json'), text)
        db = str(Path('/data/h.sqlite3').absolute())
        self.assertIn(json.dumps(f"{statusline._quote('/opt/my bin/tokenatlas')} --db {statusline._quote(db)} statusline")[1:-1], text)
        if os.name != 'nt':
            self.assertIn("'/opt/my bin/tokenatlas' --db /data/h.sqlite3 statusline", text)
        else:
            self.assertIn('\\"/opt/my bin/tokenatlas\\"', text)  # double quotes, escaped inside the JSON snippet

    def test_windows_warns_about_unsafe_characters(self):
        plain = statusline.setup_text(None, r'C:\Program Files\tokenatlas.exe', windows=True)
        self.assertNotIn('Warning', plain)
        self.assertIn(json.dumps('"C:\\Program Files\\tokenatlas.exe" statusline')[1:-1], plain)
        for path in (r'C:\a&b\tokenatlas.exe', r'C:\100%\tokenatlas.exe'):
            self.assertIn('Warning', statusline.setup_text(None, path, windows=True))
        self.assertIn('Warning', statusline.setup_text(r'C:\data&x\h.sqlite3', r'C:\bin\tokenatlas.exe', windows=True))
        self.assertNotIn('Warning', statusline.setup_text(None, '/opt/a&b/tokenatlas', windows=False))  # POSIX quoting is safe

    def test_setup_through_the_cli(self):
        done = subprocess.run([sys.executable, '-m', 'tokenatlas', 'statusline', '--setup'], capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT), CLAUDE_CONFIG_DIR='/nonexistent/claude'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(str(Path('/nonexistent/claude') / 'settings.json'), done.stdout)
        self.assertIn('"statusLine"', done.stdout)


if __name__ == '__main__':
    unittest.main()
