"""Real isolated-wheel tests for TokenAtlas upgrade and rollback."""

import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


_spec = importlib.util.find_spec("tokenatlas")
if _spec is None or _spec.origin is None:
    raise RuntimeError("run with PYTHONPATH set to the TokenAtlas source checkout")
ROOT = Path(_spec.origin).resolve().parents[1]
try:
    import setuptools
    SETUPTOOLS_MAJOR = int(setuptools.__version__.split(".", 1)[0])
except (ImportError, ValueError):
    SETUPTOOLS_MAJOR = 0
BUILD_TOOLS_AVAILABLE = importlib.util.find_spec("wheel") is not None and SETUPTOOLS_MAJOR >= 77
VERSIONS = ("1.22.0", "1.22.1")


def run(command, cwd, env=None, expected=0, stdin=None):
    result = subprocess.run(
        [str(item) for item in command], cwd=cwd, env=env,
        stdin=stdin, text=True, capture_output=True,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expected}: {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def build_wheels(work):
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
    version_file = source / "tokenatlas" / "__init__.py"
    original = version_file.read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__ = "[^"]+"$', original)
    if match is None:
        raise AssertionError("TokenAtlas version declaration was not found")
    version_line = match.group(0)
    for version in VERSIONS:
        version_file.write_text(
            original.replace(version_line, f'__version__ = "{version}"', 1), encoding="utf-8",
        )
        run([
            sys.executable, "-m", "pip", "wheel", "--no-deps",
            "--no-build-isolation", "--wheel-dir", wheels, source,
        ], work)
    version_file.write_text(original, encoding="utf-8")
    built = {}
    for wheel in wheels.glob("tokenatlas-*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            metadata_text = archive.read(metadata_name).decode("utf-8")
        for version in VERSIONS:
            if f"Version: {version}\n" in metadata_text:
                built[version] = wheel
    if set(built) != set(VERSIONS):
        raise AssertionError(f"expected synthetic wheels for {VERSIONS}; found {sorted(built)}")
    add_tzdata_wheel(wheels)
    return wheels


def add_tzdata_wheel(wheels):
    """Provide a tiny local tzdata distribution for Windows' conditional dependency."""
    path = wheels / "tzdata-2024.1-py3-none-any.whl"
    files = {
        "tzdata/__init__.py": b'"""Synthetic local dependency for isolated installer tests."""\n',
        "tzdata-2024.1.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: tzdata\nVersion: 2024.1\n"
            b"Requires-Python: >=3.7\n\n"
        ),
        "tzdata-2024.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    rows = []
    for name, payload in files.items():
        digest = hashlib.sha256(payload).digest()
        import base64
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        rows.append((name, "sha256=" + encoded, str(len(payload))))
    rows.append(("tzdata-2024.1.dist-info/RECORD", "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files["tzdata-2024.1.dist-info/RECORD"] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def restricted_environment(work, manager_paths, decoys):
    """Use disposable homes and an explicit PATH; never inherit user Python/pip config."""
    home = work / "home"
    config = home / "config"
    state = home / "state"
    home.mkdir(exist_ok=True)
    config.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    history_dir = state / "tokenatlas"
    history_dir.mkdir(exist_ok=True)
    history = history_dir / "history.sqlite3"
    history.write_bytes(b"sentinel history bytes\x00\xff")
    sentinel_config = config / "sentinel.toml"
    sentinel_config.write_bytes(b"sentinel config bytes\x00\xff")
    path_entries = list(dict.fromkeys([*(str(path) for path in manager_paths), str(decoys), os.defpath]))
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
        "PATH": os.pathsep.join(path_entries),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INPUT": "1",
        "TMPDIR": str(work),
        "TEMP": str(work),
        "TMP": str(work),
        "PYTHONNOUSERSITE": "1",
        "UV_NO_CONFIG": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(work / "uv-cache"),
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env, history, sentinel_config


def manager_tools_from_path():
    tools = {}
    for name in ("pipx", "uv"):
        executable = shutil.which(name)
        if not executable:
            raise AssertionError(f"TOKENATLAS_TEST_MANAGERS=1 requires {name} on PATH")
        tools[name] = Path(executable).absolute()
    return tools


def manager_environment(work, env, manager):
    if manager == "pipx":
        root = work / "pipx"
        env.update({
            "PIPX_HOME": str(root),
            "PIPX_BIN_DIR": str(work / "pipx-bin"),
            "PIPX_MAN_DIR": str(work / "pipx-man"),
            "PIPX_DEFAULT_BACKEND": "uv",
        })
    elif manager == "uv":
        env.update({
            "UV_TOOL_DIR": str(work / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(work / "uv-bin"),
        })
    return env


class UpgradeArtifactTests(unittest.TestCase):
    @unittest.skipUnless(BUILD_TOOLS_AVAILABLE, "release smoke requires setuptools>=77 and wheel")
    def test_real_environment_upgrade_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            wheels = build_wheels(work)
            if os.environ.get("TOKENATLAS_TEST_MANAGERS") == "1":
                tools = manager_tools_from_path()
                manager_cases = [("venv", None), ("pipx", tools["pipx"]), ("uv", tools["uv"])]
            else:
                manager_cases = [("venv", None)]
            for manager, executable in manager_cases:
                with self.subTest(manager=manager):
                    self.exercise_manager_case(work / manager, wheels, manager, executable)

    def exercise_manager_case(self, work, wheels, manager, executable):
        work.mkdir()
        decoys = work / "decoys"
        decoys.mkdir()
        marker = work / "decoy-called"
        if os.name != "nt":
            for name in ("python", "pip"):
                script = decoys / name
                script.write_text(
                    "#!/bin/sh\nprintf '%s\\n' called >> \"$DECOY_MARKER\"\nexit 99\n",
                    encoding="utf-8",
                )
                script.chmod(0o755)
        manager_paths = [executable.parent] if executable else []
        clean_env, history, sentinel_config = restricted_environment(work, manager_paths, decoys)
        clean_env["DECOY_MARKER"] = str(marker)
        clean_env = manager_environment(work, clean_env, manager)
        base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()

        if manager == "venv":
            prefix = work / "venv"
            run([sys.executable, "-m", "venv", prefix], work)
            python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            launcher = prefix / ("Scripts/tokenatlas.exe" if os.name == "nt" else "bin/tokenatlas")
            run([
                python, "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                "--find-links", wheels, "tokenatlas==1.22.0",
            ], work, clean_env)
        elif manager == "uv":
            prefix = work / "uv-tools" / "tokenatlas"
            launcher = work / "uv-bin" / ("tokenatlas.exe" if os.name == "nt" else "tokenatlas")
            run([
                executable, "--no-config", "tool", "install", "--python", base_python,
                "--no-index", "--find-links", wheels, "tokenatlas==1.22.0",
            ], work, clean_env)
            python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        else:
            prefix = work / "pipx" / "venvs" / "tokenatlas"
            launcher = work / "pipx-bin" / ("tokenatlas.exe" if os.name == "nt" else "tokenatlas")
            run([
                executable, "install", "--skip-maintenance", "--backend", "uv",
                "--python", base_python, "--pip-args", "--find-links " + shlex.quote(str(wheels)),
                "tokenatlas==1.22.0",
            ], work, clean_env)
            python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.assertTrue(python.is_file(), f"{manager} did not create its environment Python")
        self.assertEqual(run([launcher, "--version"], work, clean_env).stdout.strip(), "tokenatlas 1.22.0")

        invocation = r'''
import shlex, sys
from unittest.mock import patch
from tokenatlas import upgrade
from tokenatlas.__main__ import _main
manager, executable, wheels = sys.argv[1:4]
real_command = upgrade.command
def local_command(installation, target):
    cmd = real_command(installation, target)
    index = cmd.index("--index-url")
    del cmd[index:index+2]
    if manager == "pipx":
        return cmd + ["--pip-args", shlex.join(["--no-index", "--find-links", wheels])]
    return cmd + ["--no-index", "--find-links", wheels]
with patch.object(upgrade, "command", side_effect=local_command):
    status = _main(["upgrade", "--version", sys.argv[4], "--yes"])
raise SystemExit(status)
'''
        def cli(target):
            run([
                python, "-I", "-c", invocation, manager, str(executable or ""),
                str(wheels), target,
            ], work, clean_env)

        no_yes = r'''
from unittest.mock import patch
from tokenatlas import upgrade
from tokenatlas.__main__ import _main
with patch.object(upgrade, "command", side_effect=AssertionError("must not build install command")):
    status = _main(["upgrade", "--version", "1.22.1"])
raise SystemExit(status)
'''
        run([python, "-I", "-c", no_yes], work, clean_env, expected=2, stdin=subprocess.DEVNULL)
        self.assertEqual(run([launcher, "--version"], work, clean_env).stdout.strip(), "tokenatlas 1.22.0")
        self.assertEqual(list(prefix.parent.glob(".tokenatlas-backup-*")), [])

        cli("1.22.1")
        self.assertEqual(run([launcher, "--version"], work, clean_env).stdout.strip(), "tokenatlas 1.22.1")
        backups = list(prefix.parent.glob(".tokenatlas-backup-*"))
        self.assertEqual(len(backups), 1)
        first_backup = backups[0]
        first_receipt = json.loads((first_backup / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(first_receipt["environment"], str(prefix.resolve()))
        self.assertTrue(any(Path(path).as_posix().endswith("/tokenatlas-1.22.0.dist-info/METADATA") for path in first_receipt["sha256"]))
        self.assert_backup_hashes(first_backup, first_receipt)
        old_metadata = next((first_backup / "environment").glob("**/tokenatlas-1.22.0.dist-info/METADATA"))
        self.assertIn("Version: 1.22.0\n", old_metadata.read_text(encoding="utf-8"))

        cli("1.22.0")
        self.assertEqual(run([launcher, "--version"], work, clean_env).stdout.strip(), "tokenatlas 1.22.0")
        created = set(prefix.parent.glob(".tokenatlas-backup-*")) - {first_backup}
        self.assertEqual(len(created), 1)
        rollback_backup = created.pop()
        rollback_receipt = json.loads((rollback_backup / "recovery.json").read_text(encoding="utf-8"))
        self.assertTrue(any(Path(path).as_posix().endswith("/tokenatlas-1.22.1.dist-info/METADATA") for path in rollback_receipt["sha256"]))
        self.assert_backup_hashes(rollback_backup, rollback_receipt)
        new_metadata = next((rollback_backup / "environment").glob("**/tokenatlas-1.22.1.dist-info/METADATA"))
        self.assertIn("Version: 1.22.1\n", new_metadata.read_text(encoding="utf-8"))
        self.assertFalse(marker.exists(), "PATH decoy python/pip was invoked")
        self.assertEqual(history.read_bytes(), b"sentinel history bytes\x00\xff")
        self.assertEqual(sentinel_config.read_bytes(), b"sentinel config bytes\x00\xff")
        self.assertEqual(list((work / "home" / "state" / "tokenatlas").iterdir()), [history])

    def assert_backup_hashes(self, backup, receipt):
        for relative, digest in receipt["sha256"].items():
            self.assertEqual(
                hashlib.sha256((backup / "environment" / relative).read_bytes()).hexdigest(),
                digest,
                relative,
            )


if __name__ == "__main__":
    unittest.main()
