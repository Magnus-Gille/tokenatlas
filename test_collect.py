"""tokenatlas collect: kernel lock, step order, remote-sync timeout, exit codes. Fakes only; no real host is contacted."""
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
POSIX = os.name != 'nt'

FAKE_SYNC = r'''#!/usr/bin/env bash
echo "sync $(python3 -c 'import time;print(time.time())') hosts=${REMOTE_HOSTS_OVERRIDE:-} db=${TOKENATLAS_DB:-} report_exists=$([ -f "$STATE/report.html" ] && echo 1 || echo 0)" >> "$CALLS"
case "${FAKE_MODE:-ok}" in
  fail) echo "fake failure" >&2; exit 3;;
  hang) echo $$ > "$SYNC_PID"; sleep 1000 & echo $! > "$CHILD_PID"; wait;;
esac
exit 0
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # macOS sandboxes (CI) report EPERM for a pid that is gone or a zombie, same as ESRCH
    return True


class CollectBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.state = self.tmp / 'state' / 'tokenatlas'
        self.state.mkdir(parents=True)
        self.calls = self.tmp / 'calls'
        self.child_pid = self.tmp / 'child.pid'
        self.sync = self.tmp / 'fake_sync.sh'
        self.sync.write_text(FAKE_SYNC)
        self.sync.chmod(0o755)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ('REMOTE_HOSTS_OVERRIDE', 'TOKENATLAS_REMOTE_SYNC')}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), XDG_STATE_HOME=str(self.tmp / 'state'),
                   STATE=str(self.state), CALLS=str(self.calls), CHILD_PID=str(self.child_pid), SYNC_PID=str(self.tmp / 'sync.pid'),
                   PYTHONPATH=str(ROOT), **extra)
        return env

    def popen(self, *args, **extra):
        pre = []
        if args and args[0] == '--db':
            pre, args = list(args[:2]), args[2:]
        return subprocess.Popen([sys.executable, '-m', 'tokenatlas', *pre, 'collect', *args], env=self.env(**extra), cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def collect(self, *args, timeout=60, **extra):
        proc = self.popen(*args, **extra)
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def steps(self, out):
        """Step names in log order: 'refresh exit=0 (0.1s)' lines to ['refresh', ...]."""
        return re.findall(r'^\S+ collect: (.+?) exit=', out, re.M)

    def sync_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class CollectTest(CollectBase):
    def test_no_hosts_runs_refresh_and_report_only(self):
        proc = self.collect()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])
        self.assertTrue((self.state / 'report.html').exists())
        self.assertEqual(self.sync_calls(), [])
        self.assertTrue((self.state / 'collect.lock').exists(), 'the lock file is kept')

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_step_order_with_keep_text_and_sync(self):
        (self.state / 'top-prompts.json').write_text('{"version":2,"k":5,"by":"cost","entries":[]}\n')
        (self.state / 'top-prompts.json').chmod(0o600)
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'top', 'report', 'remote sync', 'report after sync'])
        calls = self.sync_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('hosts=pi:myhost', calls[0])
        self.assertIn('report_exists=1', calls[0], 'the first report was built before the sync')
        self.assertRegex(proc.stdout, r'refresh exit=0 \(\d+\.\ds\)')

    def test_top_keeps_the_stored_k_and_by(self):
        from tokenatlas import prompt_store
        store = self.state / 'top-prompts.json'
        for k, by in ((10, 'tokens'), (3, 'cost'), (7, 'tokens')):
            store.write_text('{"version":2,"k":%d,"by":"%s","entries":[]}\n' % (k, by))
            store.chmod(0o600)
            proc = self.collect()
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn('top', self.steps(proc.stdout))
            self.assertEqual(prompt_store.load_meta(store)[1:], (k, by))

    def test_top_is_skipped_and_the_store_untouched_without_a_valid_choice(self):
        store = self.state / 'top-prompts.json'
        for k, by in (('"x"', '"cost"'), ('true', '"cost"'), ('0', '"cost"'), ('-3', '"cost"'), ('5', '"nope"'), ('5', '7'), ('null', 'null')):
            raw = ('{"version":2,"k":%s,"by":%s,"entries":[]}\n' % (k, by)).encode()
            store.write_bytes(raw)
            store.chmod(0o600)
            proc = self.collect()
            self.assertEqual(proc.returncode, 1, (k, by, proc.stdout + proc.stderr))
            self.assertIn('top: skipped: %s has no valid recorded k/by' % store, proc.stdout)
            self.assertNotIn('top', self.steps(proc.stdout))
            self.assertEqual(store.read_bytes(), raw, (k, by))

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_top_is_skipped_for_an_unsafe_store(self):
        store = self.state / 'top-prompts.json'
        raw = b'{"version":2,"k":3,"by":"cost","entries":[]}\n'
        store.write_bytes(raw)
        store.chmod(0o644)
        proc = self.collect()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('top: skipped:', proc.stdout)
        self.assertNotIn('top', self.steps(proc.stdout))
        self.assertEqual(store.read_bytes(), raw)

    def test_top_only_when_opted_in(self):
        proc = self.collect()
        self.assertNotIn('top', self.steps(proc.stdout))

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_second_report_runs_when_sync_fails(self):
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync), FAKE_MODE='fail')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report', 'remote sync', 'report after sync'])
        self.assertIn('remote sync exit=3', proc.stdout)

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_no_report_skips_both_reports(self):
        proc = self.collect('--no-report', '--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'remote sync'])
        self.assertFalse((self.state / 'report.html').exists())

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_hosts_from_env_and_file(self):
        self.collect('--remote-sync', str(self.sync), REMOTE_HOSTS_OVERRIDE='a:h1 b:h2')
        self.assertIn('hosts=a:h1 b:h2', self.sync_calls()[0])
        self.calls.unlink()
        (self.state / 'remote-hosts').write_text('c:h3\nd:h4\n')
        self.collect('--remote-sync', str(self.sync))
        self.assertIn('hosts=c:h3 d:h4', self.sync_calls()[0])

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_remote_sync_env_override_and_packaged_default(self):
        self.collect('--remote', 'pi:myhost', TOKENATLAS_REMOTE_SYNC=str(self.sync))
        self.assertEqual(len(self.sync_calls()), 1)
        from tokenatlas import collect
        self.assertTrue(collect.PACKAGED_SYNC.is_file())

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_missing_sync_script_is_a_failure(self):
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.tmp / 'nope.sh'))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(self.steps(proc.stdout)[-1], 'report after sync')

    @unittest.skipUnless(POSIX, 'process groups are POSIX only')
    def test_sync_timeout_kills_process_group_and_still_reports(self):
        start = time.monotonic()
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync), '--sync-timeout', '2', FAKE_MODE='hang')
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('remote sync: timeout after 2s', proc.stdout)
        self.assertEqual(self.steps(proc.stdout)[-1], 'report after sync')
        pid = int(self.child_pid.read_text())
        time.sleep(0.3)
        self.assertFalse(alive(pid), 'the sleeping grandchild survived the timeout')

    def wait_for(self, path, secs=15):
        deadline = time.monotonic() + secs
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists(), f'{path.name} never appeared')
        time.sleep(0.2)
        return int(path.read_text().split()[0])

    def reap_group(self):
        sync_pid = self.tmp / 'sync.pid'
        if sync_pid.exists():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(int(sync_pid.read_text()), signal.SIGKILL)

    @unittest.skipIf(os.name == 'nt', 'the remote sync is a bash script; collect skips it on Windows')
    def test_custom_db_reaches_the_sync(self):
        custom = self.tmp / 'other' / 'my.sqlite3'
        proc = self.collect('--db', str(custom), '--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f'db={custom}', self.sync_calls()[0])
        self.assertTrue(custom.exists())

    @unittest.skipUnless(POSIX, 'process groups and flock are POSIX only')
    def test_lock_outlives_a_killed_collector_while_the_sync_runs(self):
        self.addCleanup(self.reap_group)
        proc = self.popen('--remote', 'pi:myhost', '--remote-sync', str(self.sync), FAKE_MODE='hang')
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(), proc.stderr.close()))
        child = self.wait_for(self.child_pid)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        second = self.collect()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already running', second.stdout)
        self.assertTrue(alive(child))

    @unittest.skipUnless(POSIX, 'signals and process groups are POSIX only')
    def test_sigterm_terminates_the_sync_tree(self):
        self.addCleanup(self.reap_group)
        proc = self.popen('--remote', 'pi:myhost', '--remote-sync', str(self.sync), FAKE_MODE='hang')
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(), proc.stderr.close()))
        child = self.wait_for(self.child_pid)
        leader = self.wait_for(self.tmp / 'sync.pid')
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 143)
        self.assertFalse(alive(child))
        self.assertFalse(alive(leader))
        self.assertEqual(self.collect().returncode, 0)  # lock free again

    @unittest.skipUnless(POSIX and shutil.which('bash'), 'the packaged script needs bash and POSIX groups')
    def test_timeout_leaves_no_survivor_with_the_packaged_script_and_term_resistant_ssh(self):
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        pids = self.tmp / 'ssh.pids'
        for name, body in (('ssh', f'trap "" TERM; echo $$ >> "{pids}"; sleep 1000 & echo $! >> "{pids}"; wait'),
                           ('rsync', 'exit 0'), ('scp', 'exit 0'), ('tokenatlas', 'exit 0')):
            (bindir / name).write_text(f'#!/bin/bash\n{body}\n')
            (bindir / name).chmod(0o755)
        self.addCleanup(lambda: [os.kill(int(x), signal.SIGKILL) for x in pids.read_text().split() if alive(int(x))] if pids.exists() else None)
        proc = self.collect('--remote', 'a:h1', '--sync-timeout', '3', PATH=f'{bindir}:/usr/bin:/bin', timeout=60)
        self.assertIn('remote sync: timeout after 3s', proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1)
        time.sleep(0.3)
        found = [int(x) for x in pids.read_text().split()]
        self.assertTrue(found)
        self.assertEqual([x for x in found if alive(x)], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows behaviour')
    def test_windows_skips_remote_sync_but_reports(self):
        proc = self.collect('--remote-sync', str(self.sync), REMOTE_HOSTS_OVERRIDE='a:h1')
        self.assertIn('remote sync skipped', proc.stdout)
        self.assertFalse(self.calls.exists() and self.sync_calls(), proc.stdout)


HOLDER = '''import fcntl,sys,time
f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('held',flush=True);time.sleep(1000)'''


@unittest.skipUnless(POSIX, 'flock holder is POSIX only')
class CollectLockTest(CollectBase):
    def hold(self):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.state / 'collect.lock')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def test_held_lock_skips_everything_and_exits_zero(self):
        self.hold()
        proc = self.collect('--remote', 'pi:myhost', '--remote-sync', str(self.sync))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('collect: already running', proc.stdout)
        self.assertEqual(self.steps(proc.stdout), [])
        self.assertEqual(self.sync_calls(), [])
        self.assertFalse((self.state / 'report.html').exists())

    def test_lock_released_after_holder_is_killed(self):
        holder = self.hold()
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        proc = self.collect()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('already running', proc.stdout)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])

    def test_lock_released_after_normal_run(self):
        self.collect()
        proc = self.collect()
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])


WIN_HOLDER = '''import msvcrt,sys
f=open(sys.argv[1],'a+');msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);print('held',flush=True);sys.stdin.read()'''


@unittest.skipUnless(os.name == 'nt', 'msvcrt.locking is Windows only')
class CollectWindowsLockTest(CollectBase):
    def test_msvcrt_contention_then_release(self):
        holder = subprocess.Popen([sys.executable, '-c', WIN_HOLDER, str(self.state / 'collect.lock')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        busy = self.collect()
        self.assertEqual(busy.returncode, 0, busy.stdout + busy.stderr)
        self.assertIn('already running', busy.stdout)
        holder.stdin.close()
        holder.wait(timeout=20)
        proc = self.collect()
        self.assertNotIn('already running', proc.stdout)
        self.assertEqual(self.steps(proc.stdout), ['refresh', 'report'])




@unittest.skipIf(os.name == 'nt', 'the remote sync and its second report are skipped on Windows')
class CollectReportArgsTest(CollectBase):
    """In-process, with the steps' work replaced: which arguments each report step gets (#49)."""

    def test_both_reports_build_at_most_once_an_hour(self):
        import argparse
        from unittest import mock
        from tokenatlas import collect
        for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))
        calls = []
        args = argparse.Namespace(db=self.state / 'history.sqlite3', remote=['pi:myhost'], remote_sync=str(self.sync), sync_timeout=60,
                                  no_report=False, lang='auto')
        with mock.patch.object(collect, '_quiet', lambda call, *argv: calls.append(argv) or 0), \
             mock.patch('tokenatlas.__main__.refresh_all', return_value={'status': 'ok'}), \
             mock.patch.object(collect, '_sync', return_value=0):
            self.assertEqual(collect.run(args), 0)
        reports = [a for a in calls if 'report' in a]
        self.assertEqual(len(reports), 2, calls)
        for argv in reports:
            self.assertIn('--if-changed', argv)
            self.assertEqual(argv[argv.index('--max-age') + 1], '1h', argv)

@unittest.skipUnless(POSIX, 'signals and process groups are POSIX only')
class CollectSignalWindowTest(CollectBase):
    """A real signal delivered inside the critical steps of _sync, in-process: it must never leave the sync tree running unsupervised."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from tokenatlas import collect
        self.c, self.mock = collect, mock
        for name in ('SIGTERM', 'SIGINT'):
            old = signal.signal(getattr(signal, name), collect._on_signal)
            self.addCleanup(signal.signal, getattr(signal, name), old)
        self.addCleanup(collect._DEFER.update, depth=0, pending=None)
        env = self.env(FAKE_MODE='hang')
        patcher = mock.patch.dict(os.environ, {k: env[k] for k in ('CALLS', 'CHILD_PID', 'SYNC_PID', 'STATE', 'FAKE_MODE')})
        patcher.start()
        self.addCleanup(patcher.stop)
        lock = open(self.tmp / 'lock', 'a+')
        self.addCleanup(lock.close)
        self.lock_fd = lock.fileno()

    def group_gone(self, pid):
        self.addCleanup(self.kill_group, pid)  # a regression must not leave the tree holding the test runner's output open
        # macOS CI sometimes answers EPERM instead of ESRCH for a group whose leader has exited (#113): both mean "gone".
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)

    @staticmethod
    def kill_group(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)

    def test_signal_while_the_sync_is_being_started(self):
        real, started = subprocess.Popen, []
        def popen(*a, **k):
            proc = real(*a, **k)
            started.append(proc)
            os.kill(os.getpid(), signal.SIGTERM)  # delivered before Popen returns to _sync
            return proc
        with self.mock.patch.object(self.c.subprocess, 'Popen', popen):
            with self.assertRaises(self.c.Terminated) as cm:
                self.c._sync(self.sync, 'pi:myhost', 60, self.tmp / 'db', self.lock_fd)
        self.assertEqual(cm.exception.sig, signal.SIGTERM)
        self.group_gone(started[0].pid)

    def test_second_signal_while_the_group_is_being_stopped(self):
        real, seen = self.c._stop_group, []
        def stop(proc, grace=self.c.GRACE):
            seen.append(proc.pid)
            os.kill(os.getpid(), signal.SIGINT)  # must not cut the TERM-wait-KILL-reap short
            return real(proc, grace)
        with self.mock.patch.object(self.c, '_stop_group', stop):
            with self.assertRaises(self.c.Terminated) as cm:
                self.c._sync(self.sync, 'pi:myhost', 1, self.tmp / 'db', self.lock_fd)
        self.assertEqual(cm.exception.sig, signal.SIGINT)
        self.group_gone(seen[0])
        child = int(self.child_pid.read_text())
        time.sleep(0.2)
        self.assertFalse(alive(child))

    def test_signal_while_waiting_stops_the_group(self):
        def later():
            time.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)
        import threading
        threading.Thread(target=later, daemon=True).start()
        start = time.monotonic()
        with self.assertRaises(self.c.Terminated):
            self.c._sync(self.sync, 'pi:myhost', 60, self.tmp / 'db', self.lock_fd)
        self.assertLess(time.monotonic() - start, 15)
        self.group_gone(int((self.tmp / 'sync.pid').read_text()))
        self.assertFalse(alive(int(self.child_pid.read_text())))

    def test_stop_group_treats_permission_error_like_a_vanished_group(self):
        # The group leader has exited and the OS answers EPERM (seen on macOS CI): _stop_group must finish, not raise (#113).
        proc = self.mock.Mock(pid=424242)
        with self.mock.patch.object(self.c.os, 'killpg', side_effect=PermissionError(1, 'Operation not permitted')) as kp:
            self.c._stop_group(proc, grace=1)
        self.assertEqual([c.args[1] for c in kp.call_args_list], [signal.SIGTERM, 0, signal.SIGKILL])
        proc.wait.assert_called_once()


if __name__ == '__main__':
    unittest.main()
