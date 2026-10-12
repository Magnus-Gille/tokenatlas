import base64
import contextlib
import io
import json
import math
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tokenatlas import statusline


ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
ENV = dict(os.environ, PYTHONPATH=str(ROOT))


def run_cli(db, *args, input=b"", env=None, timeout=15):
    command = [PYTHON, "-m", "tokenatlas", "--db", str(db), *args]
    return subprocess.run(command, input=input, capture_output=True, cwd=ROOT,
                          env=env or ENV, timeout=timeout)


def payload_bytes():
    return json.dumps({
        "model": {"display_name": "Synthetic"},
        "session_id": "wrapper-test",
        "rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": 1790000000},
            "seven_day": {"used_percentage": 34, "resets_at": 1790500000},
        },
    }, separators=(",", ":")).encode("utf-8")


def json_objects(text):
    decoder = json.JSONDecoder()
    found = []
    for offset, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


class StatuslineWrapperSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "state" / "history.sqlite3"
        self.db.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_executable_has_controlled_error_on_both_launch_paths(self):
        command = str(self.root / 'missing-command')
        native = run_cli(self.db, 'statusline', '--wrap', '--', command, input=payload_bytes())
        helper = subprocess.run([PYTHON, '-I', '-S', '-c', statusline._WRAP_HELPER],
                                input=json.dumps([command]).encode() + b'\n' + payload_bytes(), capture_output=True)
        for result in (native, helper):
            self.assertEqual(result.returncode, 127)
            self.assertIn(b'cannot run command', result.stderr)
            self.assertNotIn(b'Traceback', result.stderr)

    def test_wrap_forwards_raw_stdin_and_stdout_stderr_bytes(self):
        child = (
            "import sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'err\\x00\\xff')"
        )
        raw = b"\x00binary\xff\x80 with spaces\n"
        done = run_cli(self.db, "statusline", "--wrap", "--", PYTHON, "-c", child, input=raw)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, raw)
        self.assertEqual(done.stderr, b"err\x00\xff")

    def test_deeply_nested_json_cannot_prevent_raw_command_passthrough(self):
        raw = b'[' * 2000 + b'0' + b']' * 2000
        child = 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(23)'
        done = run_cli(self.db, 'statusline', '--wrap', '--', PYTHON, '-c', child, input=raw)
        self.assertEqual(done.returncode, 23, done.stderr)
        self.assertEqual(done.stdout, raw)
        self.assertEqual(done.stderr, b'')

    def test_wrap_passes_through_child_exit_code_and_records_before_child(self):
        marker = self.root / "child-observed.txt"
        quota = self.db.parent / statusline.QUOTA_NAME
        child = (
            "import pathlib,sys; q=pathlib.Path(sys.argv[2]); "
            "pathlib.Path(sys.argv[1]).write_text(\"before=\" + str(q.is_file()) + \"\\nargs=\" + repr(sys.argv[3:])); "
            "sys.exit(23)"
        )
        child_args = [PYTHON, "-c", child, str(marker), str(quota), "--no-record-quota",
                      "quoted ' value; $(touch should-not-exist)", "operator & value"]
        done = run_cli(self.db, "statusline", "--wrap", "--wrap-timeout", "2", "--", *child_args,
                       input=payload_bytes())
        self.assertEqual(done.returncode, 23)
        observed = marker.read_text()
        self.assertIn("before=True", observed)
        self.assertIn("--no-record-quota", observed)
        self.assertIn("quoted ' value; $(touch should-not-exist)", observed)
        self.assertTrue(quota.is_file())
        self.assertNotIn("should-not-exist", {p.name for p in self.root.iterdir()})

    def test_wrap_timeout_returns_124_with_diagnostic_and_promptly_finishes(self):
        child = "import time; time.sleep(30)"
        started = time.monotonic()
        done = run_cli(self.db, "statusline", "--wrap", "--wrap-timeout", "0.1", "--", PYTHON,
                       "-c", child, input=payload_bytes(), timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 124)
        self.assertLess(elapsed, 3)
        self.assertIn(b"timeout", done.stderr.lower())

    def test_wrap_timeout_handles_second_communicate_timeout_with_bounded_wait(self):
        class DoubleTimeoutProcess:
            pid = 1
            returncode = -9

            def __init__(self):
                self.communicate_timeouts = []
                self.wait_timeouts = []

            def communicate(self, *args, **kwargs):
                timeout = kwargs.get("timeout")
                self.communicate_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(["synthetic-child"], timeout)

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                if timeout is None:
                    raise AssertionError("wrapped-process cleanup wait must be bounded")
                self.returncode = -9
                return self.returncode

        process = DoubleTimeoutProcess()
        stderr = io.StringIO()
        with mock.patch.object(statusline.subprocess, "Popen", return_value=process), \
                mock.patch.object(statusline, "_stop_wrapped"), \
                mock.patch('tokenatlas.upgrade_process._Job'), \
                contextlib.redirect_stderr(stderr):
            result = statusline.run(
                ["--no-record-quota", "--wrap", "--", "synthetic-child"],
                db=self.db,
                stdin=io.BytesIO(payload_bytes()),
            )

        self.assertEqual(result, 124)
        self.assertEqual(process.communicate_timeouts[0], 5)
        self.assertTrue(all(
            isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0
            for timeout in process.communicate_timeouts
        ))
        self.assertTrue(process.wait_timeouts)
        self.assertTrue(all(
            isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0
            for timeout in process.wait_timeouts
        ))
        self.assertIn("timeout", stderr.getvalue().lower())
        self.assertNotIn("Traceback", stderr.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_timeout_kills_grandchild_that_holds_output_pipes(self):
        child = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        done = run_cli(self.db, "statusline", "--wrap", "--wrap-timeout", "0.1", "--", PYTHON,
                       "-c", child, input=payload_bytes(), timeout=6)
        elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 124)
        self.assertLess(elapsed, 3)
        self.assertIn(b"timeout", done.stderr.lower())

    def test_normal_exit_closes_descendant_output_pipes_promptly(self):
        grandchild = (
            "import sys,time; sys.stdout.write('grandchild stdout'); "
            "sys.stderr.write('grandchild stderr'); sys.stdout.flush(); sys.stderr.flush(); time.sleep(4)"
        )
        child = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {grandchild!r}])"
        started = time.monotonic()
        done = run_cli(self.db, "statusline", "--wrap", "--wrap-timeout", "0.1", "--", PYTHON,
                       "-c", child, input=payload_bytes(), timeout=6)
        elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(elapsed, 3)

    def test_default_wrap_timeout_is_five_seconds(self):
        child = "import time; time.sleep(5.2)"
        started = time.monotonic()
        done = run_cli(self.db, "statusline", "--wrap", "--", PYTHON, "-c", child,
                       input=payload_bytes(), timeout=8)
        elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 124)
        self.assertGreaterEqual(elapsed, 4.5)
        self.assertLess(elapsed, 7)

    def test_wrap_timeout_must_be_finite_positive_and_at_most_sixty(self):
        child = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')"
        marker = self.root / "ran"
        for value in ("0", "-1", "nan", "inf", "60.1"):
            done = run_cli(self.db, "statusline", "--wrap", "--wrap-timeout", value, "--", PYTHON,
                           "-c", child, str(marker), input=payload_bytes())
            self.assertNotEqual(done.returncode, 0, value)
            self.assertFalse(marker.exists(), value)

    def _isolated_signal_env(self):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        return {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
            "TOKENATLAS_NO_QUOTA": "1",
        }

    @staticmethod
    def _pid_is_live(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            try:
                state = proc_stat.read_text().rsplit(") ", 1)[1].split()[0]
            except (OSError, IndexError):
                return True
            return state != "Z"
        try:
            probe = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True, check=False)
        except OSError:
            return True
        state = probe.stdout.strip()
        return bool(state) and not state.startswith("Z")

    @classmethod
    def _wait_pid_gone(cls, pid, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not cls._pid_is_live(pid):
                return True
            time.sleep(0.02)
        return not cls._pid_is_live(pid)

    @staticmethod
    def _group_is_live(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _wait_group_gone(cls, pgid, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not cls._group_is_live(pgid):
                return True
            time.sleep(0.02)
        return not cls._group_is_live(pgid)

    @staticmethod
    def _cleanup_signal_process(process, child_pid=None, child_pgid=None):
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid is not None and StatuslineWrapperSubprocessTests._pid_is_live(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    @unittest.skipIf(os.name == "nt", "POSIX process groups and signals")
    def test_sighup_after_child_start_returns_129_and_kills_child_group(self):
        marker = self.root / "child-pid-and-pgid"
        child = (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}\\n{os.getpgid(0)}\\n'); "
            "time.sleep(30)"
        )
        env = self._isolated_signal_env()
        process = subprocess.Popen(
            [PYTHON, "-m", "tokenatlas", "--db", str(self.db), "statusline", "--no-record-quota",
             "--wrap", "--", PYTHON, "-c", child, str(marker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=ROOT, env=env,
        )
        child_pid = child_pgid = None
        try:
            process.stdin.write(payload_bytes())
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.is_file(), "child did not publish its PID")
            child_pid, child_pgid = (int(value) for value in marker.read_text().splitlines())
            os.kill(process.pid, signal.SIGHUP)
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 128 + signal.SIGHUP)
            self.assertTrue(self._wait_pid_gone(child_pid), "child process survived SIGHUP")
            self.assertTrue(self._wait_group_gone(child_pgid), "child process group survived SIGHUP")
        finally:
            self._cleanup_signal_process(process, child_pid, child_pgid)

    @unittest.skipIf(os.name == "nt", "POSIX process groups and signals")
    def test_signal_during_popen_return_returns_signal_code_and_kills_child(self):
        child = "import time; time.sleep(30)"
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=signal.Signals(signum).name):
                child_pid_file = self.root / f"startup-child-{signum}"
                env = self._isolated_signal_env()
                env.update({
                    "TOKENATLAS_SIGNAL": str(int(signum)),
                    "TOKENATLAS_CHILD_PID_FILE": str(child_pid_file),
                    "TOKENATLAS_DB": str(self.db),
                })
                harness = (
                    "import os,signal,subprocess,sys\n"
                    "from pathlib import Path\n"
                    "from tokenatlas import statusline\n"
                    "real_popen=subprocess.Popen\n"
                    "def launch(*args,**kwargs):\n"
                    "    process=real_popen(*args,**kwargs)\n"
                    "    Path(os.environ['TOKENATLAS_CHILD_PID_FILE']).write_text(str(process.pid))\n"
                    "    os.kill(os.getpid(),int(os.environ['TOKENATLAS_SIGNAL']))\n"
                    "    return process\n"
                    "statusline.subprocess.Popen=launch\n"
                    "raise SystemExit(statusline.run(['--no-record-quota','--wrap','--',sys.executable,'-c',"
                    f"{child!r}],db=Path(os.environ['TOKENATLAS_DB']),stdin=sys.stdin.buffer))"
                )
                process = subprocess.Popen(
                    [PYTHON, "-c", harness], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env,
                )
                child_pid = None
                try:
                    process.stdin.write(payload_bytes())
                    process.stdin.close()
                    process.stdin = None
                    process.wait(timeout=5)
                    self.assertEqual(process.returncode, 128 + signum)
                    self.assertTrue(child_pid_file.is_file(), "Popen did not publish child PID")
                    child_pid = int(child_pid_file.read_text())
                    self.assertTrue(self._wait_pid_gone(child_pid), "startup child survived signal")
                    self.assertTrue(self._wait_group_gone(child_pid), "startup child group survived signal")
                finally:
                    if child_pid is None and child_pid_file.is_file():
                        child_pid = int(child_pid_file.read_text())
                    self._cleanup_signal_process(process, child_pid, child_pid)

    @unittest.skipIf(os.name == "nt", "POSIX sh setup")
    def test_setup_wrap_prints_effective_settings_and_undo_without_writing(self):
        config = self.root / "claude"
        config.mkdir()
        base = {
            "statusLine": {
                "type": "command",
                "command": "printf '%s' 'base value' && printf '%s' 'base&operator'",
                "padding": 3,
                "refreshInterval": 11,
            },
            "permissions": {"allow": ["Read"]},
        }
        local = {
            "statusLine": {
                "type": "command",
                "command": "printf '%s' \"local 'quoted' value\" && printf '%s' 'local&operator'",
                "padding": 5,
                "refreshInterval": 17,
            },
            "theme": "dark",
        }
        settings = config / "settings.json"
        settings_local = config / "settings.local.json"
        settings.write_text(json.dumps(base, indent=2) + "\n")
        settings_local.write_text(json.dumps(local, indent=2) + "\n")
        before = {p: p.read_bytes() for p in (settings, settings_local)}
        env = dict(ENV, CLAUDE_CONFIG_DIR=str(config))
        done = run_cli(self.db, "statusline", "--setup", "--wrap", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        text = done.stdout.decode()
        add_instruction = text[:text.index("\n\n")]
        undo_start = text.index("Undo:")
        self.assertIn(str(settings_local), add_instruction)
        self.assertIn(str(settings_local), text[undo_start:])
        objects = json_objects(text)
        status_objects = [obj["statusLine"] for obj in objects if isinstance(obj.get("statusLine"), dict)]
        wrapped = [line for line in status_objects if "--wrap" in str(line.get("command", ""))]
        undo = [line for line in status_objects if line.get("command") == local["statusLine"]["command"]]
        self.assertTrue(wrapped, text)
        self.assertTrue(undo, text)
        self.assertEqual(wrapped[0]["padding"], 5)
        self.assertEqual(wrapped[0]["refreshInterval"], 17)
        self.assertEqual(undo[0], local["statusLine"])
        self.assertIn("undo", text.lower())
        self.assertIn("sh -c", text)
        shell_command = wrapped[0]["command"]
        shell_tokens = shlex.split(shell_command)
        self.assertIn("/bin/sh", shell_tokens)
        shell_index = shell_tokens.index("/bin/sh")
        self.assertEqual(shell_tokens[shell_index + 1], "-c")
        self.assertIn(local["statusLine"]["command"], shell_tokens[shell_index + 2:])
        executed = subprocess.run(["sh", "-c", shell_command], input=payload_bytes(), capture_output=True,
                                  cwd=ROOT, env=env, timeout=5)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout, b"local 'quoted' valuelocal&operator")
        self.assertEqual(executed.stderr, b"")
        self.assertTrue((self.db.parent / statusline.QUOTA_NAME).is_file())

    @unittest.skipIf(os.name == "nt", "POSIX shell setup")
    def test_setup_wrap_refuses_compound_existing_tokenatlas_command(self):
        config = self.root / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "cd /srv/project && tokenatlas statusline"}
        }))
        done = run_cli(self.db, "statusline", "--setup", "--wrap",
                       env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b"already uses TokenAtlas", done.stderr)
        self.assertNotIn(b'"--wrap"', done.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX shell setup")
    def test_setup_wrap_allows_quoted_tokenatlas_literal(self):
        config = self.root / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "printf '%s' 'tokenatlas statusline'"}
        }))
        done = run_cli(self.db, "statusline", "--setup", "--wrap",
                       env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b'--wrap', done.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX launcher quoting")
    def test_setup_wrap_quotes_spaced_interpreter_module_fallback(self):
        config = self.root / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "printf '%s' spaced"}
        }))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}, clear=False), \
                mock.patch.object(statusline, "executable", return_value="/tmp/Python Runtime/bin/python -m tokenatlas"):
            text = statusline.wrap_setup_text(self.db)
        wrapped = [obj["statusLine"] for obj in json_objects(text)
                   if isinstance(obj.get("statusLine"), dict) and "--wrap" in obj["statusLine"].get("command", "")]
        self.assertTrue(wrapped)
        tokens = shlex.split(wrapped[0]["command"])
        self.assertEqual(tokens[:3], ["/tmp/Python Runtime/bin/python", "-m", "tokenatlas"])

    @unittest.skipIf(os.name != "nt", "cmd.exe quoting is exercised on Windows")
    def test_setup_wrap_uses_inert_base64_cmd_c_on_windows(self):
        self.db = self.root / 'state(~)+path' / 'history.sqlite3'
        self.db.parent.mkdir()
        config = self.root / "claude"
        config.mkdir()
        original_source = 'echo "quoted ^& operator %TOKENATLAS_WRAP_MARKER%"'
        original = {"statusLine": {"type": "command", "command": original_source, "padding": 2,
                                    "refreshInterval": 9}}
        (config / "settings.json").write_text(json.dumps(original))
        env = dict(ENV, CLAUDE_CONFIG_DIR=str(config), TOKENATLAS_WRAP_MARKER="synthetic")
        done = run_cli(self.db, "statusline", "--setup", "--wrap", "--wrap-shell", "cmd", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        wrapped = [obj["statusLine"] for obj in json_objects(done.stdout.decode())
                   if isinstance(obj.get("statusLine"), dict) and "--wrap" in obj["statusLine"].get("command", "")]
        self.assertTrue(wrapped)
        wrapped_command = wrapped[0]["command"]
        encoded = wrapped_command.rsplit("--wrap-shell-base64 ", 1)[1].split()[0]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), original_source)
        self.assertEqual(wrapped[0]["padding"], 2)
        self.assertEqual(wrapped[0]["refreshInterval"], 9)
        shutil.copy2(sys.executable, self.root / "cmd.exe")
        shutil.copy2(sys.executable, self.root / "taskkill.exe")
        env["PATH"] = str(self.root) + os.pathsep + env.get("PATH", "")
        system_cmd = statusline._system_command("cmd.exe")
        # cmd.exe consumes source, not CRT-escaped argv. /s strips these outer
        # quotes and preserves the snippet's path quotes and metacharacters.
        outer = subprocess.list2cmdline([system_cmd, '/d', '/s', '/c']) + ' "' + wrapped_command + '"'
        executed = subprocess.run(outer, executable=system_cmd, input=payload_bytes(),
                                  capture_output=True, cwd=ROOT, env=env, timeout=5)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        original_output = subprocess.run(original_source, shell=True, executable=system_cmd,
                                         input=payload_bytes(), capture_output=True, env=env, cwd=ROOT, timeout=5)
        self.assertEqual(original_output.returncode, 0, original_output.stderr)
        self.assertEqual(executed.stdout, original_output.stdout)
        self.assertEqual(executed.stderr, b"")
        self.assertTrue((self.db.parent / statusline.QUOTA_NAME).is_file())

    def test_wrap_shell_base64_requires_an_explicit_shell(self):
        encoded = base64.b64encode(b"printf '%s' encoded").decode("ascii")
        done = run_cli(self.db, "statusline", "--wrap", "--wrap-shell-base64", encoded,
                       input=payload_bytes())
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b"requires --wrap-shell", done.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows shell selection")
    def test_setup_wrap_on_windows_requires_an_explicit_shell(self):
        config = self.root / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "echo original"}
        }))
        done = run_cli(self.db, "statusline", "--setup", "--wrap",
                       env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b"choose --wrap-shell", done.stderr)

    @unittest.skipUnless(os.name == 'nt', 'native cmd path validation')
    def test_cmd_setup_refuses_expanding_database_path(self):
        config = self.root / 'claude'
        config.mkdir()
        (config / 'settings.json').write_text(json.dumps({
            'statusLine': {'type': 'command', 'command': 'echo original'}
        }))
        done = run_cli(self.root / '%UNTRUSTED%' / 'history.sqlite3', 'statusline',
                       '--setup', '--wrap', '--wrap-shell', 'cmd',
                       env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b'cmd metacharacters', done.stderr)

    @unittest.skipUnless(os.name == "nt", "native Windows PowerShell")
    def test_setup_wrap_preserves_native_powershell_source(self):
        config = self.root / "claude"
        config.mkdir()
        source = '$quoted = "quoted \' value"; if ($true) { Write-Output ("$quoted|" + $env:TOKENATLAS_WRAP_MARKER) }'
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": source}
        }))
        env = dict(ENV, CLAUDE_CONFIG_DIR=str(config), TOKENATLAS_WRAP_MARKER="native-marker")
        done = run_cli(self.db, "statusline", "--setup", "--wrap", "--wrap-shell", "powershell", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        wrapped = [obj["statusLine"] for obj in json_objects(done.stdout.decode())
                   if isinstance(obj.get("statusLine"), dict) and "--wrap" in obj["statusLine"].get("command", "")]
        self.assertEqual(len(wrapped), 1, done.stdout)
        wrapped_command = wrapped[0]["command"]
        self.assertIn("--wrap-shell powershell", wrapped_command)
        encoded = wrapped_command.rsplit("--wrap-shell-base64 ", 1)[1].split()[0]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), source)
        self.assertEqual(statusline._command_state(wrapped_command), 'enabled')
        executed = subprocess.run(statusline._source_command('powershell', wrapped_command),
                                  input=payload_bytes(), capture_output=True, cwd=ROOT, env=env, timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertIn(b"quoted ' value|native-marker", executed.stdout)

    @unittest.skipUnless(os.name == "nt", "native Windows Git Bash")
    def test_setup_wrap_preserves_native_git_bash_source(self):
        config = self.root / "claude"
        config.mkdir()
        source = 'values=("quoted value" second); if [[ "${#values[@]}" -eq 2 ]]; then printf "%s\\n" "${values[@]}" | tr " " "_"; fi'
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": source}
        }))
        done = run_cli(self.db, "statusline", "--setup", "--wrap", "--wrap-shell", "bash",
                       env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)))
        self.assertEqual(done.returncode, 0, done.stderr)
        wrapped = [obj["statusLine"] for obj in json_objects(done.stdout.decode())
                   if isinstance(obj.get("statusLine"), dict) and "--wrap" in obj["statusLine"].get("command", "")]
        self.assertEqual(len(wrapped), 1, done.stdout)
        wrapped_command = wrapped[0]["command"]
        self.assertIn("--wrap-shell bash", wrapped_command)
        encoded = wrapped_command.rsplit("--wrap-shell-base64 ", 1)[1].split()[0]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), source)
        self.assertEqual(statusline._command_state(wrapped_command), 'enabled')
        executed = subprocess.run(statusline._source_command('bash', wrapped_command),
                                  input=payload_bytes(), capture_output=True, cwd=ROOT,
                                  env=dict(ENV, CLAUDE_CONFIG_DIR=str(config)), timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertIn(b"quoted_value", executed.stdout)
        self.assertIn(b"second", executed.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX Bash selection")
    def test_setup_wrap_default_sh_refuses_bash_specific_source(self):
        config = self.root / "claude"
        config.mkdir()
        source = 'values=(one two); if [[ "${values[0]}" == one ]]; then printf "%s\\n" "${values[@]}" | tr " " "_"; fi'
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": source}
        }))
        env = dict(ENV, CLAUDE_CONFIG_DIR=str(config))
        refused = run_cli(self.db, "statusline", "--setup", "--wrap", env=env)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn(b"Bash-specific", refused.stderr)

        selected = run_cli(self.db, "statusline", "--setup", "--wrap", "--wrap-shell", "bash", env=env)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        wrapped = [obj["statusLine"] for obj in json_objects(selected.stdout.decode())
                   if isinstance(obj.get("statusLine"), dict) and "--wrap" in obj["statusLine"].get("command", "")]
        self.assertEqual(len(wrapped), 1, selected.stdout)
        shell_tokens = shlex.split(wrapped[0]["command"])
        self.assertIn("/bin/bash", shell_tokens)
        self.assertIn(source, shell_tokens)
        executed = subprocess.run(['/bin/bash', '-c', wrapped[0]['command']],
                                  input=payload_bytes(), capture_output=True, cwd=ROOT, env=env, timeout=5)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout, b'one\ntwo\n')

    def test_doctor_recognizes_a_wrapped_tokenatlas_statusline(self):
        config = self.root / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "tokenatlas statusline --wrap -- sh -c 'printf \"%s\" \"inner & operator --no-record-quota\"'"}
        }))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}, clear=False):
            self.assertIs(statusline.recording_configured(), True)
            (config / "settings.json").write_text(json.dumps({
                "statusLine": {"type": "command", "command": "tokenatlas statusline --wrap && echo outer -- sh -c printf"}
            }))
            self.assertEqual(statusline.recording_state(), "unknown")

    def test_classifier_preserves_windows_paths_and_common_launchers(self):
        commands = [r'C:\Users\me\.local\bin\tokenatlas.exe statusline',
                    r'C:\Users\me\.local\bin\tokenatlas.exe --db C:\state\history.sqlite3 statusline',
                    'uvx tokenatlas statusline', 'uv tool run tokenatlas statusline',
                    'pipx run tokenatlas statusline', 'nice -n 5 tokenatlas statusline',
                    'timeout 2 tokenatlas statusline', 'uvx tokenatlas@1.23 statusline',
                    'py -m tokenatlas statusline', 'uvx --from tokenatlas tokenatlas statusline']
        config = self.root / 'claude'
        config.mkdir()
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(statusline._command_state(command), 'enabled')
                (config / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': command}}))
                with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}, clear=False):
                    with self.assertRaisesRegex(ValueError, 'already uses TokenAtlas'):
                        statusline.wrap_setup_text(self.db)

    def test_launcher_arguments_and_mixed_quoted_literals_remain_foreign(self):
        commands = ["nice -n 5 printf '%s %s\\n' 'tokenatlas' 'statusline'",
                    'timeout 2 my-status --app tokenatlas --view statusline',
                    'echo x"(tokenatlas statusline)"', 'echo x"y "tokenatlas statusline',
                    "uvx my-status --app 'tokenatlas' --view 'statusline'"]
        config = self.root / 'claude'
        config.mkdir()
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(statusline._command_state(command), 'foreign')
                (config / 'settings.json').write_text(json.dumps({'statusLine': {'type': 'command', 'command': command}}))
                with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}, clear=False):
                    self.assertIn('--wrap', statusline.wrap_setup_text(self.db, shell='sh' if os.name != 'nt' else 'powershell'))

    def test_quoted_operators_do_not_create_command_positions(self):
        for command in ("printf ';' tokenatlas statusline", "printf '>' tokenatlas statusline",
                        'echo "|" tokenatlas statusline', 'echo "( tokenatlas statusline )"'):
            with self.subTest(command=command):
                self.assertEqual(statusline._command_state(command), 'foreign')
        self.assertEqual(statusline._command_state('tokenatlas >out statusline'), 'unknown')
        self.assertEqual(statusline._command_state('>out tokenatlas statusline'), 'unknown')

    def test_classifier_does_not_scan_quoted_literals_or_launcher_operands(self):
        foreign = [
            r'echo " C:\x\" tokenatlas statusline"',
            r'echo " C:\x\" ; tokenatlas statusline ; echo C:\y\" "',
            'python3 status.py -m tokenatlas statusline',
            'py render.py -m tokenatlas statusline',
            "python -c 'print(1)' -m tokenatlas statusline",
            'nice -n 5 python3 status.py -m tokenatlas statusline',
            "sh render.sh -c 'tokenatlas statusline'",
            'tokenatlas report --project statusline',
            "printf '%s' >tokenatlas statusline",
        ]
        for command in foreign:
            with self.subTest(command=command):
                self.assertEqual(statusline._command_state(command), 'foreign')

        quoted_literals = [
            r"printf '%s' 'tokenatlas statusline'",
            r'''printf "%s" "tokenatlas statusline"''',
            r'''printf '%s' 'token'"atlas statusline"''',
            r'''printf '%s' 'C:\tools\tokenatlas' "statusline"''',
            r'''echo " C:\x\" tokenatlas statusline"''',
        ]
        for command in quoted_literals:
            with self.subTest(quoted_literal=command):
                shlex.split(command)  # All matrix entries are valid POSIX shell words.
                self.assertEqual(statusline._command_state(command), 'foreign')

        enabled = [
            'python -I -S -W ignore -X dev -m tokenatlas statusline',
            'py -3.12 -m tokenatlas statusline',
        ]
        for command in enabled:
            with self.subTest(interpreter_flags=command):
                self.assertEqual(statusline._command_state(command), 'enabled')

    def test_windows_helper_interpreter_resolution_refuses_venv_redirector(self):
        with mock.patch.object(sys, 'prefix', str(self.root)), \
                mock.patch.object(sys, 'base_prefix', str(self.root / 'base')), \
                mock.patch.object(sys, '_base_executable', str(self.root / 'python.exe')):
            with self.assertRaisesRegex(OSError, 'external Python'):
                statusline._wrapper_python()
        base = self.root.parent / 'base-python.exe'
        with mock.patch.object(sys, 'prefix', str(self.root)), \
                mock.patch.object(sys, 'base_prefix', str(self.root.parent)), \
                mock.patch.object(sys, '_base_executable', str(base)):
            self.assertEqual(statusline._wrapper_python(), base.resolve())

    @unittest.skipUnless(os.name == 'nt', 'native Windows venv helper supervision')
    def test_windows_venv_wrapper_uses_base_interpreter_and_forwards_bytes(self):
        venv = self.root / 'venv'
        subprocess.run([PYTHON, '-m', 'venv', '--without-pip', str(venv)], check=True, capture_output=True)
        python = venv / 'Scripts' / 'python.exe'
        result = subprocess.run([str(python), '-m', 'tokenatlas', '--db', str(self.db),
                                 'statusline', '--no-record-quota', '--wrap', '--', PYTHON, '-c',
                                 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(23)'],
                                input=b'\x00raw\xff', capture_output=True, cwd=ROOT, env=ENV, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, b'\x00raw\xff')

    def test_plain_statusline_behavior_is_unchanged(self):
        done = run_cli(self.db, "statusline", input=payload_bytes())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"Synthetic | 5h:12% 7d:34%" + os.linesep.encode())

    def test_missing_or_empty_wrap_command_is_an_error(self):
        for args in (("statusline", "--wrap"), ("statusline", "--wrap", "--")):
            done = run_cli(self.db, *args, input=payload_bytes())
            self.assertNotEqual(done.returncode, 0, args)
            self.assertNotEqual(done.stdout, b"TokenAtlas\n", args)


if __name__ == "__main__":
    unittest.main()
