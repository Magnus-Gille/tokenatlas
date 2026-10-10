"""Isolated ownership-detection tests for tokenatlas.upgrade."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tokenatlas import upgrade


class DetectionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "base-python"
        self.base.mkdir()
        self.base_python = self.base / "python"
        self.base_python.touch()
        self.prefix = self.root / "env"
        self._make_prefix(self.prefix)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.bin = self.root / "manager-bin"
        self.bin.mkdir()
        self.python = self.prefix / "bin" / "python"
        self.launcher = self.prefix / "bin" / "tokenatlas"
        self.module = self.prefix / "lib" / "python3.13" / "site-packages" / "tokenatlas" / "upgrade.py"
        self.distribution = Mock()
        self.distribution.version = upgrade.__version__
        self.distribution.read_text.return_value = None
        self.distribution.locate_file.return_value = self.module.parent
        self._activate_patches()
        self.query_results = {}
        self.query_error = None
        self.which_missing = set()

    def _make_prefix(self, prefix):
        (prefix / "bin").mkdir(parents=True)
        (prefix / "pyvenv.cfg").write_text("home = isolated-fixture\n")
        (prefix / "bin" / "python").touch()
        (prefix / "bin" / "tokenatlas").touch()
        (prefix / "lib" / "python3.13" / "site-packages" / "tokenatlas").mkdir(parents=True)
        (prefix / "lib" / "python3.13" / "site-packages" / "tokenatlas" / "upgrade.py").touch()

    def _activate_patches(self):
        self.patches = []
        for target, name, value in (
            (upgrade.sys, "prefix", str(self.prefix)),
            (upgrade.sys, "base_prefix", str(self.base)),
            (upgrade.sys, "executable", str(self.python)),
            (upgrade.sys, "_base_executable", str(self.base_python)),
            (upgrade, "__file__", str(self.module)),
            (upgrade.sysconfig, "get_path", lambda name: str(self.module.parent.parent)),
            (upgrade.metadata, "distribution", lambda name: self.distribution),
            (upgrade.metadata, "distributions", lambda: [self._dist("pip"), self._dist("setuptools")]),
            (upgrade.importlib.util, "find_spec", lambda name: object()),
            (upgrade.shutil, "which", self._which),
            (upgrade, "_query", self._query),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.patches.append(p)
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        while self.patches:
            self.patches.pop().stop()

    @staticmethod
    def _dist(name):
        return Mock(metadata={"Name": name})

    def _which(self, name):
        if name in self.which_missing:
            return None
        return str(self.tools / name)

    def _query(self, argv):
        if self.query_error is not None:
            raise self.query_error
        key = tuple(str(part) for part in argv[1:])
        if key not in self.query_results:
            raise AssertionError(f"unexpected manager query: {key!r}")
        return str(self.query_results[key])

    def _manager(self, manager, *, owner_root=None, exposed_matches=True):
        self.prefix = self.root / "pipx-home" / "venvs" / "tokenatlas" if manager == "pipx" else (
            self.root / "uv-tools" / "tokenatlas" if manager == "uv" else self.root / "env"
        )
        self._make_prefix(self.prefix)
        self.python = self.prefix / "bin" / "python"
        self.launcher = self.prefix / "bin" / "tokenatlas"
        self.module = self.prefix / "lib" / "python3.13" / "site-packages" / "tokenatlas" / "upgrade.py"
        # Replace the fixture's initially patched paths with this manager's paths.
        self._replace_patch(upgrade.sys, "prefix", str(self.prefix))
        self._replace_patch(upgrade.sys, "executable", str(self.python))
        self._replace_patch(upgrade, "__file__", str(self.module))
        self.distribution.locate_file.return_value = self.module.parent

        if manager == "pipx":
            (self.prefix / "pipx_metadata.json").write_text(json.dumps({
                "main_package": {"package": "tokenatlas", "apps": ["tokenatlas"]},
            }))
            home = owner_root or self.root / "pipx-home"
            bindir = self.root / "pipx-bin"
            bindir.mkdir(exist_ok=True)
            self.query_results[("environment", "--value", "PIPX_HOME")] = home
            self.query_results[("environment", "--value", "PIPX_BIN_DIR")] = bindir
            executable = self.tools / "pipx"
        elif manager == "uv":
            (self.prefix / "uv-receipt.toml").write_text("version = 1\n")
            home = owner_root or self.root / "uv-tools"
            bindir = self.root / "uv-bin"
            bindir.mkdir(exist_ok=True)
            self.query_results[("tool", "dir")] = home
            self.query_results[("tool", "dir", "--bin")] = bindir
            executable = self.tools / "uv"
        else:
            executable = None
        if manager in ("pipx", "uv"):
            exposed = bindir / self.launcher.name
            if exposed_matches:
                os.link(self.launcher, exposed)
            else:
                exposed.touch()
        return executable

    def _replace_patch(self, target, name, value):
        existing = next(p for p in self.patches if p.target is target and p.attribute == name)
        existing.stop()
        replacement = patch.object(target, name, value)
        replacement.start()
        self.patches[self.patches.index(existing)] = replacement

    def _detect(self):
        return upgrade.detect()


class DetectPositiveTests(DetectionFixture):
    def test_dedicated_venv_is_detected_and_command_targets_its_python(self):
        installation = self._detect()
        self.assertEqual(installation.manager, "venv")
        self.assertEqual(installation.prefix, self.prefix.resolve())
        self.assertEqual(upgrade.command(installation, "2.4.0"), [
            str(self.python), "-I", "-m", "pip", "--isolated", "install", "--upgrade",
            "--no-user", "--prefix", str(self.prefix.resolve()),
            "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "tokenatlas==2.4.0",
        ])

    def test_pipx_ownership_is_confirmed_and_command_uses_pipx(self):
        executable = self._manager("pipx")
        installation = self._detect()
        self.assertEqual(installation.manager, "pipx")
        self.assertEqual(installation.manager_exe, executable)
        self.assertEqual(upgrade.command(installation, "2.4.0"), [
            str(executable), "install", "--force", "--upgrade", "--index-url",
            "https://pypi.org/simple", "tokenatlas==2.4.0",
        ])

    def test_uv_ownership_is_confirmed_and_command_uses_uv(self):
        executable = self._manager("uv")
        installation = self._detect()
        self.assertEqual(installation.manager, "uv")
        self.assertEqual(installation.manager_exe, executable)
        self.assertEqual(upgrade.command(installation, "2.4.0"), [
            str(executable), "--no-config", "tool", "install", "--python", str(self.base_python.resolve()),
            "--no-python-downloads", "--index-url", "https://pypi.org/simple", "tokenatlas==2.4.0",
        ])


class DetectRefusalTests(DetectionFixture):
    def assert_unsupported(self, message=None):
        with self.assertRaises(upgrade.Unsupported) as caught:
            self._detect()
        if message:
            self.assertIn(message, str(caught.exception))

    def test_system_python_is_refused(self):
        with patch.object(upgrade.sys, "base_prefix", str(self.prefix)):
            self.assert_unsupported("system or externally managed")

    def test_external_management_marker_inside_venv_is_refused(self):
        (self.prefix / "EXTERNALLY-MANAGED").touch()
        self.assert_unsupported("externally managed")

    def test_stdlib_marker_inside_venv_is_refused(self):
        stdlib=self.prefix/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'
        stdlib.mkdir(parents=True,exist_ok=True)
        (stdlib/'EXTERNALLY-MANAGED').touch()
        self.assert_unsupported('externally managed')

    def test_externally_managed_base_does_not_disqualify_actual_venv(self):
        stdlib=self.root/'base-stdlib';stdlib.mkdir()
        (stdlib/'EXTERNALLY-MANAGED').touch()
        with patch.object(upgrade.sysconfig,'get_path',return_value=str(stdlib)):
            self.assertEqual(self._detect().manager,'venv')

    def test_source_checkout_module_is_refused(self):
        with patch.object(upgrade, "__file__", str(self.root / "checkout" / "tokenatlas" / "upgrade.py")):
            self.assert_unsupported("source checkout")

    def test_editable_source_distribution_is_refused(self):
        self.distribution.read_text.return_value = json.dumps({
            "url": "file:///private/source/tokenatlas", "dir_info": {"editable": True},
        })
        self.assert_unsupported("editable/source")

    def test_metadata_version_mismatch_is_refused(self):
        self.distribution.version = "999.0.0"
        self.assert_unsupported("metadata and imported version disagree")

    def test_missing_pipx_manager_is_refused_without_pip_fallback(self):
        self._manager("pipx")
        self.which_missing.add("pipx")
        with patch.object(upgrade.importlib.util, "find_spec", side_effect=AssertionError("pip fallback")):
            self.assert_unsupported("pipx is missing")

    def test_dual_manager_receipts_are_refused(self):
        self._manager("pipx")
        (self.prefix / "uv-receipt.toml").touch()
        self.assert_unsupported("ambiguous pipx and uv ownership")

    def test_mismatched_manager_tool_root_is_refused(self):
        executable = self._manager("uv", owner_root=self.root / "other-uv-tools")
        self.assert_unsupported("does not own the active environment")
        self.assertEqual(executable.name, "uv")

    def test_wrong_manager_entrypoint_is_refused(self):
        self._manager("pipx", exposed_matches=False)
        self.assert_unsupported("does not match this environment")

    def test_shared_venv_is_refused(self):
        with patch.object(upgrade.metadata, "distributions", return_value=[
            self._dist("tokenatlas"), self._dist("other-application"),
        ]):
            self.assert_unsupported("environment contains other applications")

    def test_manager_query_error_does_not_fall_back_to_venv(self):
        self._manager("uv")
        self.query_error = upgrade.Unsupported("manager could not confirm ownership")
        with patch.object(upgrade.importlib.util, "find_spec", side_effect=AssertionError("pip fallback")):
            self.assert_unsupported("could not confirm ownership")


if __name__ == "__main__":
    unittest.main()
