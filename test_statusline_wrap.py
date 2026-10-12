import base64
import json
import os
import shlex
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
        self.assertEqual(executed.stdout, b'"quoted & operator synthetic"\r\n')
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
        self.assertEqual(done.stdout, b"Synthetic | 5h:12% 7d:34%\n")

    def test_missing_or_empty_wrap_command_is_an_error(self):
        for args in (("statusline", "--wrap"), ("statusline", "--wrap", "--")):
            done = run_cli(self.db, *args, input=payload_bytes())
            self.assertNotEqual(done.returncode, 0, args)
            self.assertNotEqual(done.stdout, b"TokenAtlas\n", args)


if __name__ == "__main__":
    unittest.main()
