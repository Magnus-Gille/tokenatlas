import base64
import json
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
                    "TOKENATLAS_SIGNAL": str(signum),
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
        self.assertIn("sh", shell_tokens)
        shell_index = shell_tokens.index("sh")
        self.assertEqual(shell_tokens[shell_index + 1], "-c")
        self.assertIn(local["statusLine"]["command"], shell_tokens[shell_index + 2:])
        executed = subprocess.run(["sh", "-c", shell_command], input=payload_bytes(), capture_output=True,
                                  cwd=ROOT, env=env, timeout=5)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout, b"local 'quoted' valuelocal&operator")
        self.assertEqual(executed.stderr, b"")
        self.assertTrue((self.db.parent / statusline.QUOTA_NAME).is_file())

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
        config = self.root / "claude"
        config.mkdir()
        original_source = 'echo "quoted ^& operator %TOKENATLAS_WRAP_MARKER%"'
        original = {"statusLine": {"type": "command", "command": original_source, "padding": 2,
                                    "refreshInterval": 9}}
        (config / "settings.json").write_text(json.dumps(original))
        env = dict(ENV, CLAUDE_CONFIG_DIR=str(config), TOKENATLAS_WRAP_MARKER="synthetic")
        done = run_cli(self.db, "statusline", "--setup", "--wrap", env=env)
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
        executed = subprocess.run([system_cmd, "/d", "/s", "/c", wrapped_command], input=payload_bytes(),
                                  capture_output=True, cwd=ROOT, env=env, timeout=5)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        original_output = subprocess.run(original_source, shell=True, executable=system_cmd,
                                         input=payload_bytes(), capture_output=True, env=env, cwd=ROOT, timeout=5)
        self.assertEqual(original_output.returncode, 0, original_output.stderr)
        self.assertEqual(executed.stdout, original_output.stdout)
        self.assertEqual(executed.stderr, b"")
        self.assertTrue((self.db.parent / statusline.QUOTA_NAME).is_file())

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
