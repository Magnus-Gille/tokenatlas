"""Smoke-test the built distribution from outside the repository checkout."""

import json
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tokenatlas import __version__


ROOT = Path(__file__).resolve().parent
try:
    import setuptools
    SETUPTOOLS_MAJOR = int(setuptools.__version__.split('.', 1)[0])
except (ImportError, ValueError):
    SETUPTOOLS_MAJOR = 0
BUILD_TOOLS_AVAILABLE = importlib.util.find_spec("wheel") is not None and SETUPTOOLS_MAJOR >= 77


def run(command, cwd, env=None, expected=0):
    result = subprocess.run(
        [str(item) for item in command], cwd=cwd, env=env,
        text=True, capture_output=True,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expected}: {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class ReleaseArtifactTests(unittest.TestCase):
    @unittest.skipUnless(BUILD_TOOLS_AVAILABLE, "release smoke requires setuptools>=77 and wheel")
    def test_wheel_install_refresh_report_reinstall_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(
                ROOT / "tokenatlas", source / "tokenatlas",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            wheels = work / "wheels"
            wheels.mkdir()
            run([
                sys.executable, "-m", "pip", "wheel", "--no-deps",
                "--no-build-isolation", "--wheel-dir", wheels, source,
            ], work)
            built = list(wheels.glob("*.whl"))
            self.assertEqual(len(built), 1)
            with zipfile.ZipFile(built[0]) as archive:
                names = archive.namelist()
                self.assertIn("tokenatlas/why.py", names)
                self.assertIn("tokenatlas/report_template.html", names)
                self.assertIn("tokenatlas/efficiency.js", names)
                self.assertIn("tokenatlas/efficiency.py", names)
                self.assertIn("tokenatlas/prices.json", names)
                self.assertIn("tokenatlas/credits.json", names)
                self.assertNotIn("why.py", names)
                entry_points = archive.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
            self.assertIn("tokenatlas = tokenatlas.__main__:main", entry_points)
            self.assertIn("energy-monitor = tokenatlas.__main__:main", entry_points)

            environment = work / "venv"
            run([sys.executable, "-m", "venv", environment], work)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = environment / ("Scripts/tokenatlas.exe" if os.name == "nt" else "bin/tokenatlas")
            alias = environment / ("Scripts/energy-monitor.exe" if os.name == "nt" else "bin/energy-monitor")
            clean_env = dict(os.environ)
            clean_env.pop("PYTHONPATH", None)
            run([python, "-m", "pip", "install", built[0]], work, clean_env)

            version = run([command, "--version"], work, clean_env).stdout.strip()
            self.assertEqual(version, f"tokenatlas {__version__}")
            legacy = run([alias, "--version"], work, clean_env)
            self.assertEqual(legacy.stdout.strip(), f"tokenatlas {__version__}")
            self.assertEqual(legacy.stderr.strip(), "energy-monitor is deprecated; use tokenatlas")

            logs = work / "logs"
            logs.mkdir()
            row = {
                "type": "assistant", "uuid": "response-smoke", "requestId": "request-smoke",
                "sessionId": "session-smoke", "cwd": "/work/smoke", "version": "test",
                "timestamp": "2026-09-03T10:00:00Z",
                "message": {"id": "message-smoke", "model": "test-model", "usage": {
                    "input_tokens": 10, "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 0, "output_tokens": 5,
                }},
            }
            (logs / "session.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            database = work / "state" / "history.sqlite3"
            base = [command, "--db", database]

            imported = json.loads(run(
                [*base, "refresh", "--harness", "claude", "--root", logs], work, clean_env,
            ).stdout)
            self.assertEqual(imported["status"], "ok")
            self.assertEqual(imported["observations_seen"], 1)

            repeated = json.loads(run(
                [*base, "refresh", "--harness", "claude", "--root", logs], work, clean_env,
            ).stdout)
            self.assertEqual(repeated["files_skipped"], 1)
            self.assertEqual(json.loads(run([*base, "doctor"], work, clean_env).stdout)["observations"], 1)

            report = work / "tokenatlas.html"
            result = json.loads(run([
                *base, "report", "--timezone", "UTC", "--html", report,
            ], work, clean_env).stdout)
            self.assertEqual(result["observations"], 1)
            html = report.read_text(encoding="utf-8")
            self.assertIn('id="report-data"', html)
            self.assertNotIn("/work/smoke", html)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

            run([
                python, "-m", "pip", "install", "--no-deps", "--force-reinstall", built[0],
            ], work, clean_env)
            self.assertEqual(run([command, "--version"], work, clean_env).returncode, 0)
            run([python, "-m", "pip", "uninstall", "-y", "tokenatlas"], work, clean_env)
            probe = run([
                python, "-c", "import importlib.util; print(importlib.util.find_spec('tokenatlas'))",
            ], work, clean_env)
            self.assertEqual(probe.stdout.strip(), "None")
            self.assertTrue(database.is_file())

            destination = os.environ.get("TOKENATLAS_SMOKE_REPORT")
            if destination:
                shutil.copy2(report, destination)


if __name__ == "__main__":
    unittest.main()
