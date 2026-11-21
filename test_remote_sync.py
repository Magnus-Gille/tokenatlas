"""remote_sync.sh: entry validation and per-host failure isolation, with stubbed commands on a temp PATH."""
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SHIM = Path(__file__).with_name('remote_sync.sh')
SCRIPT = Path(__file__).with_name('tokenatlas') / 'remote_sync.sh'


def kill_group(proc):
    """Kill the whole process group of a start_new_session child and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def run_group(env, timeout=60, bash_args=()):
    proc = subprocess.Popen(['bash', *bash_args, str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding='latin-1', start_new_session=True)  # latin-1: bytes map 1:1, so raw control bytes stay visible to assertions
    try:
        out, err = proc.communicate(timeout=timeout)
    finally:
        kill_group(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


STUB = '#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n'


@unittest.skipIf(os.name == 'nt', 'remote_sync.sh targets macOS/Linux hosts')
class RemoteSyncTest(unittest.TestCase):
    def run_script(self, hosts, bodies_override=None, mv_fails_first=False, rm_fails=False, extra_env=None, em_in_home_bin=False, remote_only=None, bash_args=(), home_files=None):
        # The prefix holds the host names on purpose: scp gets a local temp path, so a stub that matches a host
        # anywhere in "$*" (rather than the "host:path" argument) fails every time instead of when the random
        # temp name happens to contain it (TOKEN-195).
        tmp = tempfile.TemporaryDirectory(prefix='remote-sync-h1-h2-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'bin').mkdir()
        (root / 'home').mkdir()
        self.home = root / 'home'
        for rel, text in (home_files or {}).items():
            (root / 'home' / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / 'home' / rel).write_text(text)
        log = root / 'log'
        counter = root / 'mv_count'
        bodies = {'ssh': 'exit 0', 'tokenatlas': 'exit 0', 'scp': 'touch "${@: -1}"',
                  'rm': 'exit 1' if rm_fails else 'exit 0',
                  'mv': (f'if [ ! -e "{counter}" ]; then touch "{counter}"; exit 1; fi; exit 0'
                         if mv_fails_first else 'exit 0')}
        bodies.update(bodies_override or {})
        if remote_only:
            # ssh really runs the remote command line in a fake remote home holding only `remote_only`.
            remote_bin = root / 'remote_home' / '.local' / 'bin'
            remote_bin.mkdir(parents=True)
            (remote_bin / remote_only).write_text(f'#!/bin/bash\necho "remote-{remote_only} $*" >> "$STUB_LOG"\nexit 0\n')
            (remote_bin / remote_only).chmod(0o755)
            bodies['ssh'] = ('HOME="' + str(root / 'remote_home') + '" PATH=/usr/bin:/bin bash -c "${@: -1}"')
        for name, body in bodies.items():
            if name == 'tokenatlas' and em_in_home_bin:
                (root / 'home' / '.local' / 'bin').mkdir(parents=True)
                stub = root / 'home' / '.local' / 'bin' / name
            else:
                stub = root / 'bin' / name
            stub.write_text(STUB.format(name=name, body=body))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {'PATH': f'{root / "bin"}:/usr/bin:/bin', 'HOME': str(root / 'home'), 'STUB_LOG': str(log),
               'REMOTE_HOSTS_OVERRIDE': hosts, **(extra_env or {})}
        proc = run_group(env, bash_args=bash_args)
        return proc, log.read_text() if log.exists() else ''

    HOSTILE = (r'\033[2J\033[H\033]52;c;ZXZpbA==\007\033]0;title\007 over\rwritten \205\233\220 \177 \302\205 caf\303\251')

    def assert_terminal_safe(self, text):
        raw = [c for c in text if (ord(c) < 32 and c not in '\t\n') or 0x7f <= ord(c) <= 0x9f or ord(c) > 0xff]
        self.assertEqual(raw, [], repr(text))
        self.assertIn('\\x1b[2J', text)
        self.assertIn('\\x1b]52;c;ZXZpbA==\\x07', text)
        self.assertIn('\\x0d', text)
        self.assertIn('\\x85', text)

    def test_hostile_ssh_snapshot_diagnostic_is_escaped(self):
        proc, _ = self.run_script('a:h1 b:h2', bodies_override={
            'ssh': f'case "$*" in *h1*snapshot*) printf "boom {self.HOSTILE}\\n" >&2; exit 3;; esac; exit 0'})
        self.assertEqual(proc.returncode, 1)
        self.assert_terminal_safe(proc.stderr)
        self.assertIn('snapshot failed on a: boom ', proc.stderr)
        self.assertIn('Syncing history from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)

    def test_hostile_scp_diagnostic_is_escaped(self):
        proc, _ = self.run_script('a:h1 b:h2', bodies_override={
            'scp': f'case "$*" in *" h1:"*) printf "{self.HOSTILE}" >&2; exit 1;; esac; touch "${{@: -1}}"'})
        self.assertEqual(proc.returncode, 1)
        self.assert_terminal_safe(proc.stderr)
        self.assertIn('scp failed for a: ', proc.stderr)
        self.assertIn('history: OK', proc.stdout)

    def test_hostile_import_diagnostic_is_escaped(self):
        proc, _ = self.run_script('a:h1 b:h2', bodies_override={
            'tokenatlas': f'case "$*" in *"--label a"*) printf "{self.HOSTILE}" >&2; exit 1;; esac; exit 0'})
        self.assertEqual(proc.returncode, 1)
        self.assert_terminal_safe(proc.stderr)
        self.assertIn('import failed for a: ', proc.stderr)
        self.assertIn('history: OK', proc.stdout)

    def test_hostile_diagnostics_stay_escaped_when_echo_interprets_escapes(self):
        for label, kwargs in (('POSIXLY_CORRECT', {'extra_env': {'POSIXLY_CORRECT': '1'}}),
                              ('xpg_echo', {'bash_args': ('-O', 'xpg_echo')})):
            for name, body in (('ssh', f'case "$*" in *h1*snapshot*) printf "{self.HOSTILE}" >&2; exit 3;; esac; exit 0'),
                               ('scp', f'case "$*" in *" h1:"*) printf "{self.HOSTILE}" >&2; exit 1;; esac; touch "${{@: -1}}"'),
                               ('tokenatlas', f'case "$*" in *"--label a"*) printf "{self.HOSTILE}" >&2; exit 1;; esac; exit 0')):
                with self.subTest(mode=label, stub=name):
                    proc, _ = self.run_script('a:h1 b:h2', bodies_override={name: body}, **kwargs)
                    self.assertEqual(proc.returncode, 1)
                    self.assert_terminal_safe(proc.stderr)
                    self.assertIn('history: OK', proc.stdout)

    def test_plain_diagnostic_stays_readable(self):
        proc, _ = self.run_script('a:h1', bodies_override={
            'scp': 'printf "Permission denied (publickey).\\n\\tsecond line\\n" >&2; exit 1'})
        self.assertIn('scp failed for a: Permission denied (publickey).\n\tsecond line\n', proc.stderr)
        self.assertNotIn('\\x', proc.stderr)

    STATE = '.local/state/tokenatlas/remote/{}.sync-state'

    def state(self, tag):
        text = (self.home / self.STATE.format(tag)).read_text()
        return {k: int(v) for k, v in (line.split('=') for line in text.splitlines())}

    def test_backoff_skips_a_host_after_repeated_failures(self):
        future = int(time.time()) + 3600
        proc, log = self.run_script('a:h1 b:h2', home_files={self.STATE.format('a'): f'fails=3\nlast_ok=1000\nlast_try=5\nnext_try={future}\n'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('history: SKIPPED a: 3 failed syncs in a row', proc.stdout)
        self.assertIn('last success', proc.stdout)
        self.assertNotIn(' -- h1 ', log)  # no ssh to the skipped host
        self.assertIn(' -- h2 ', log)
        self.assertEqual(self.state('a')['fails'], 3)  # a skip changes nothing

    def test_force_sync_ignores_backoff_and_success_resets_it(self):
        future = int(time.time()) + 3600
        proc, log = self.run_script('a:h1', extra_env={'TOKENATLAS_FORCE_SYNC': '1'},
                                    home_files={self.STATE.format('a'): f'fails=5\nlast_ok=1000\nlast_try=5\nnext_try={future}\n'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('history: OK', proc.stdout)
        state = self.state('a')
        self.assertEqual((state['fails'], state['next_try']), (0, 0))
        self.assertGreater(state['last_ok'], 1000)

    def test_expired_backoff_retries_and_a_failure_extends_it(self):
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'exit 255'},
                                  home_files={self.STATE.format('a'): 'fails=3\nlast_ok=1000\nlast_try=5\nnext_try=10\n'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('ssh exit 255', proc.stderr)
        state = self.state('a')
        self.assertEqual((state['fails'], state['last_ok']), (4, 1000))
        self.assertAlmostEqual(state['next_try'] - state['last_try'], 7200, delta=5)  # 3600 doubled once

    def test_backoff_starts_at_the_threshold_and_is_capped(self):
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'exit 255'},
                                  home_files={self.STATE.format('a'): 'fails=1\nlast_ok=0\nlast_try=5\nnext_try=0\n'})
        st = self.state('a')
        self.assertEqual((st['fails'], st['next_try']), (2, st['last_try']))  # below the threshold of 3: no wait
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'exit 255'}, extra_env={'TOKENATLAS_BACKOFF_MAX': '5000'},
                                  home_files={self.STATE.format('a'): 'fails=9\nlast_ok=0\nlast_try=5\nnext_try=0\n'})
        state = self.state('a')
        self.assertEqual(state['next_try'] - state['last_try'], 5000)

    def test_first_ever_failure_is_recorded_with_no_success_and_a_far_deadline_is_ignored(self):
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'exit 255'})
        self.assertEqual(self.state('a')['last_ok'], 0)
        far = int(time.time()) + 10 * 86400  # beyond the longest back-off: a clock jump, not a real deadline
        proc, log = self.run_script('a:h1', home_files={self.STATE.format('a'): f'fails=3\nlast_ok=0\nlast_try=5\nnext_try={far}\n'})
        self.assertIn('history: OK', proc.stdout)

    def test_leading_zero_setting_is_decimal(self):
        future = int(time.time()) + 3600
        proc, _ = self.run_script('a:h1', extra_env={'TOKENATLAS_BACKOFF_AFTER': '08'},
                                  home_files={self.STATE.format('a'): f'fails=9\nlast_ok=0\nlast_try=5\nnext_try={future}\n'})
        self.assertIn('history: SKIPPED a', proc.stdout)

    def test_backoff_can_be_disabled_and_rejects_bad_settings(self):
        future = int(time.time()) + 3600
        proc, log = self.run_script('a:h1', extra_env={'TOKENATLAS_BACKOFF_AFTER': '0'},
                                    home_files={self.STATE.format('a'): f'fails=9\nlast_ok=0\nlast_try=5\nnext_try={future}\n'})
        self.assertIn('history: OK', proc.stdout)
        proc, _ = self.run_script('a:h1', extra_env={'TOKENATLAS_BACKOFF_BASE': 'soon'})
        self.assertEqual(proc.returncode, 2)
        self.assertIn('TOKENATLAS_BACKOFF_BASE must be', proc.stderr)

    def test_invalid_pairs_rejected(self):
        proc, log = self.run_script('../../x:host pi:-oProxyCommand=x ok:good.host')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("invalid tag:host entry '../../x:host'", proc.stderr)
        self.assertIn("invalid tag:host entry 'pi:-oProxyCommand=x'", proc.stderr)
        self.assertNotIn('../../x', log)
        self.assertNotIn('ProxyCommand', log)
        self.assertIn('-- good.host', log)

    def test_failing_mv_does_not_stop_next_host(self):
        proc, log = self.run_script('a:h1 b:h2', mv_fails_first=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('cannot store snapshot for a', proc.stderr)
        self.assertIn('Syncing history from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)
        self.assertIn('tokenatlas import', log)

    def test_failing_rm_cleanup_does_not_stop_next_host(self):
        proc, log = self.run_script('a:h1 b:h2', mv_fails_first=True, rm_fails=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('cannot store snapshot for a', proc.stderr)
        self.assertIn('Syncing history from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)

    def test_import_failure_is_nonzero_but_other_host_syncs(self):
        proc, log = self.run_script('a:h1 b:h2', bodies_override={
            'tokenatlas': 'case "$*" in *"--label a"*) echo boom >&2; exit 1;; esac; exit 0'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('import failed for a', proc.stderr)
        self.assertIn('Syncing history from b (h2)', proc.stdout)
        self.assertIn('import ', log.split('--label a', 1)[1])  # b's import ran after a's failure
        self.assertIn('history: OK', proc.stdout)

    def test_snapshot_failure_is_nonzero(self):
        proc, _ = self.run_script('a:h1', bodies_override={
            'ssh': 'case "$*" in *snapshot*) echo nope >&2; exit 3;; esac; exit 0'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('snapshot failed on a', proc.stderr)

    def test_scp_failure_is_nonzero(self):
        proc, _ = self.run_script('a:h1', bodies_override={'scp': 'echo nope >&2; exit 1'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('scp failed for a', proc.stderr)

    def test_remote_without_tokenatlas_is_benign(self):
        proc, log = self.run_script('a:h1', bodies_override={'ssh': 'exit 1'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('history: not installed on a', proc.stdout)

    def test_ssh_connection_failure_on_probe_is_nonzero(self):
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'exit 255'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('history: ERROR cannot check a for tokenatlas (ssh exit 255)', proc.stderr)

    TAILSCALE_CHECK = ('# Tailscale SSH requires an additional check.\\n'
                       '# To authenticate, visit: https://login.tailscale.com/a/secret123\\n')

    def test_tailscale_check_mode_names_reauthentication_without_the_url(self):
        proc, _ = self.run_script('a:h1 b:h2', bodies_override={
            'ssh': f'case "$*" in *h1*) printf "{self.TAILSCALE_CHECK}" >&2; exit 255;; esac; exit 0'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('history: ERROR a: Tailscale SSH check needs re-authentication (run ssh h1 interactively)', proc.stderr)
        self.assertNotIn('login.tailscale.com', proc.stderr + proc.stdout)
        self.assertNotIn('secret123', proc.stderr + proc.stdout)
        self.assertNotIn('ssh exit 255', proc.stderr)
        self.assertIn('Syncing history from b (h2)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)

    def test_other_probe_stderr_is_not_logged(self):
        # Only the recognised Tailscale message changes the log line; arbitrary ssh output (which may carry hostnames
        # or URLs) is still not copied into it.
        proc, _ = self.run_script('a:h1', bodies_override={
            'ssh': 'printf "ssh: connect to host h1 port 22: Connection refused https://example.invalid/x\\n" >&2; exit 255'})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('history: ERROR cannot check a for tokenatlas (ssh exit 255)', proc.stderr)
        self.assertNotIn('example.invalid', proc.stderr + proc.stdout)

    def test_probe_stdout_still_discarded_and_install_found(self):
        proc, _ = self.run_script('a:h1', bodies_override={'ssh': 'echo /home/x/.local/bin/tokenatlas; exit 0'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('/home/x/.local/bin/tokenatlas', proc.stdout + proc.stderr)
        self.assertIn('history: OK', proc.stdout)

    def test_remote_commands_prepend_user_bin_to_path(self):
        proc, log = self.run_script('a:h1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ssh_lines = [line for line in log.splitlines() if line.startswith('ssh ') and ' -- h1 ' in line]
        self.assertEqual(len(ssh_lines), 2, ssh_lines)
        check, snap = ssh_lines
        self.assertIn('PATH="$HOME/.local/bin:$PATH"', check)
        self.assertIn('command -v tokenatlas || command -v energy-monitor', check)
        self.assertIn('PATH="$HOME/.local/bin:$PATH"', snap)
        self.assertIn('snapshot ~/.local/state/tokenatlas/snapshot.sqlite3', snap)

    def test_local_db_option_before_import(self):
        proc, log = self.run_script('a:h1', extra_env={'TOKENATLAS_DB': '/tmp/x.sqlite3'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('tokenatlas --db /tmp/x.sqlite3 import ', log)

    def test_legacy_env_db_still_honoured(self):
        proc, log = self.run_script('a:h1', extra_env={'ENERGY_MONITOR_DB': '/tmp/y.sqlite3'})
        self.assertIn('tokenatlas --db /tmp/y.sqlite3 import ', log)

    def test_remote_with_only_energy_monitor_falls_back(self):
        proc, log = self.run_script('a:h1', remote_only='energy-monitor')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('remote-energy-monitor snapshot', log)
        self.assertIn('history: OK', proc.stdout)

    def test_remote_with_tokenatlas_uses_it(self):
        proc, log = self.run_script('a:h1', remote_only='tokenatlas')
        self.assertIn('remote-tokenatlas snapshot', log)
        self.assertIn('history: OK', proc.stdout)

    def test_no_db_option_by_default(self):
        proc, log = self.run_script('a:h1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('tokenatlas import ', log)
        self.assertNotIn('--db', log)

    def test_empty_db_env_means_default(self):
        proc, log = self.run_script('a:h1', extra_env={'ENERGY_MONITOR_DB': ''})
        self.assertNotIn('--db', log)

    def test_local_user_bin_install_found_without_path(self):
        proc, log = self.run_script('a:h1', em_in_home_bin=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('not installed locally', proc.stderr)
        self.assertIn('tokenatlas import ', log)
        self.assertIn('history: OK', proc.stdout)


@unittest.skipIf(os.name == 'nt', 'remote_sync.sh targets macOS/Linux hosts')
class RemoteSyncTimeoutTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'home').mkdir()
        self.log = self.root / 'log'

    def stub(self, name, body):
        path = self.root / 'bin' / name
        path.write_text(STUB.format(name=name, body=body))
        path.chmod(0o755)

    def run_sync(self, hosts, **env):
        e = {'PATH': f'{self.root / "bin"}:/usr/bin:/bin', 'HOME': str(self.root / 'home'),
             'STUB_LOG': str(self.log), 'REMOTE_HOSTS_OVERRIDE': hosts, **env}
        start = time.monotonic()
        proc = run_group(e)
        return proc, time.monotonic() - start

    def test_stalled_host_times_out_and_next_host_runs(self):
        # ssh hangs for host "stuck" only; the healthy host answers at once.
        self.stub('ssh', f'case "$*" in *stuck*) echo $$ > "{self.root}/ssh.pid"; exec sleep 1000;; esac; exit 0')
        self.stub('tokenatlas', 'exit 0')
        self.stub('scp', 'touch "${@: -1}"')
        proc, took = self.run_sync('a:stuck b:healthy', TOKENATLAS_HOST_TIMEOUT='3')
        self.assertLess(took, 12, proc.stderr)
        self.assertGreaterEqual(took, 3)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('stuck: ERROR (timeout after 3s)', proc.stderr)
        self.assertIn('Syncing history from b (healthy)', proc.stdout)
        self.assertIn('history: OK', proc.stdout)
        pid = int((self.root / 'ssh.pid').read_text().split()[0])
        time.sleep(0.5)
        self.assertFalse(pid_alive(pid), 'killed host left its ssh behind')

    def test_term_kills_worker_group(self):
        self.stub('ssh', f'echo $$ > "{self.root}/ssh.pid"; exec sleep 1000')
        env = {'PATH': f'{self.root / "bin"}:/usr/bin:/bin', 'HOME': str(self.root / 'home'),
               'STUB_LOG': str(self.log), 'REMOTE_HOSTS_OVERRIDE': 'a:h1'}
        proc = subprocess.Popen(['bash', str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        self.addCleanup(kill_group, proc)
        pidfile = self.root / 'ssh.pid'
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(1)
        pid = int(pidfile.read_text().split()[0])
        self.assertTrue(pid_alive(pid))
        proc.send_signal(signal.SIGTERM)  # the script only, not its group
        gone = time.monotonic() + 3
        while pid_alive(pid) and time.monotonic() < gone:
            time.sleep(0.1)
        self.assertFalse(pid_alive(pid), 'fake ssh survived TERM of remote_sync.sh')
        self.assertEqual(proc.wait(timeout=5), 143)
        proc.communicate()

    def test_hanging_scp_is_killed_too(self):
        self.stub('ssh', 'exit 0')
        self.stub('scp', 'sleep 1000')
        self.stub('tokenatlas', 'exit 0')
        proc, took = self.run_sync('a:stuck', TOKENATLAS_HOST_TIMEOUT='2')
        self.assertLess(took, 10, proc.stderr)
        self.assertIn('stuck: ERROR (timeout after 2s)', proc.stderr)

    def test_calls_carry_timeout_options(self):
        for name, body in (('ssh', 'exit 0'), ('tokenatlas', 'exit 0'),
                           ('scp', 'touch "${@: -1}"')):
            self.stub(name, body)
        proc, _ = self.run_sync('a:h1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self.log.read_text().splitlines()
        for tool in ('ssh', 'scp'):
            line = next(l for l in lines if l.startswith(tool + ' '))
            for opt in ('ConnectTimeout=10', 'ServerAliveInterval=10', 'ServerAliveCountMax=3', 'BatchMode=yes'):
                self.assertIn(opt, line, line)
            self.assertIn(' -- h1', line)
        self.assertFalse([l for l in lines if l.startswith('rsync ')], lines)

    def test_ssh_opts_overridable(self):
        for name, body in (('ssh', 'exit 0'), ('tokenatlas', 'exit 0'),
                           ('scp', 'touch "${@: -1}"')):
            self.stub(name, body)
        proc, _ = self.run_sync('a:h1', TOKENATLAS_SSH_OPTS='-o ConnectTimeout=3')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self.log.read_text()
        self.assertIn('ssh -o ConnectTimeout=3 -- h1', log)
        self.assertNotIn('ServerAliveInterval', log)

    def test_bad_timeout_rejected(self):
        proc, _ = self.run_sync('a:h1', TOKENATLAS_HOST_TIMEOUT='soon')
        self.assertEqual(proc.returncode, 2)

    def test_bash_syntax(self):
        for path in (SCRIPT, SHIM):
            subprocess.run(['bash', '-n', str(path)], check=True)

    def test_shim_runs_packaged_script(self):
        self.stub('ssh', 'exit 0')
        self.stub('tokenatlas', 'exit 0')
        self.stub('scp', 'touch "${@: -1}"')
        e = {'PATH': f'{self.root / "bin"}:/usr/bin:/bin', 'HOME': str(self.root / 'home'), 'STUB_LOG': str(self.log),
             'REMOTE_HOSTS_OVERRIDE': 'a:h1'}
        proc = subprocess.run(['bash', str(SHIM)], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('history: OK', proc.stdout)


if __name__ == '__main__':
    unittest.main()
