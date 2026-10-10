"""Upgrade safety: no live installations or network are used here."""
import contextlib
import io
import json
import os
import sys
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tokenatlas import upgrade
from tokenatlas.install_lock import installation_lock, Busy


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root/'venv'
        self.prefix.mkdir()
        self.python = self.prefix/'bin/python'
        self.python.parent.mkdir()
        self.python.write_text('python')
        self.install = upgrade.Installation('venv', self.prefix, self.python, self.python.parent/'tokenatlas', None)

    def args(self, **kw):
        return SimpleNamespace(check=False, version=None, yes=True, **kw)

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = upgrade.run(args)
        return rc, out.getvalue(), err.getvalue()

    def test_check_does_not_create_lock_backup_or_touch_install(self):
        before = list(self.root.rglob('*'))
        with patch.object(upgrade, 'detect', return_value=self.install), patch.object(upgrade, 'latest_version', return_value='99.0.0'), patch.object(upgrade, 'mutate') as mutate:
            rc, out, err = self.invoke(SimpleNamespace(check=True,version=None,yes=False))
        self.assertEqual(rc, 0, err)
        self.assertIn('99.0.0', out)
        self.assertEqual(list(self.root.rglob('*')), before)
        mutate.assert_not_called()

    def test_noninteractive_requires_yes_before_backup(self):
        with patch.object(upgrade, 'detect', return_value=self.install), patch.object(upgrade, 'latest_version', return_value='99.0.0'), patch('sys.stdin.isatty',return_value=False), patch.object(upgrade,'mutate') as mutate:
            rc, _, err = self.invoke(SimpleNamespace(check=False,version=None,yes=False))
        self.assertEqual(rc, 2)
        self.assertIn('--yes', err)
        mutate.assert_not_called()

    def test_pin_bypasses_latest_and_supports_downgrade(self):
        with patch.object(upgrade, 'detect', return_value=self.install), patch.object(upgrade, 'latest_version') as latest, patch.object(upgrade,'mutate') as mutate:
            rc, _, err = self.invoke(SimpleNamespace(check=False,version='1.20.0',yes=True))
        self.assertEqual(rc, 0, err)
        latest.assert_not_called()
        mutate.assert_called_once_with(self.install, '1.20.0')

    def test_invalid_pin_never_detects_or_installs(self):
        with patch.object(upgrade,'detect') as detect:
            rc, _, _ = self.invoke(SimpleNamespace(check=False,version='--index-url=evil',yes=True))
        self.assertEqual(rc, 2)
        detect.assert_not_called()

    def test_network_failure_is_actionable(self):
        with patch.object(upgrade,'detect',return_value=self.install), patch.object(upgrade,'latest_version',side_effect=ValueError('PyPI unavailable')):
            rc, _, err = self.invoke(SimpleNamespace(check=True,version=None,yes=False))
        self.assertEqual(rc, 1)
        self.assertIn('PyPI', err)

    def test_unsupported_check_still_reports_latest(self):
        with patch.object(upgrade,'detect',side_effect=upgrade.Unsupported('source checkout')), patch.object(upgrade,'latest_version',return_value='99.0.0'):
            rc, out, _ = self.invoke(SimpleNamespace(check=True,version=None,yes=False))
        self.assertEqual(rc, 0)
        self.assertIn('source checkout', out)

    def test_busy_install_does_not_mutate(self):
        with installation_lock(self.prefix), patch.object(upgrade,'detect',return_value=self.install), patch.object(upgrade,'mutate') as mutate:
            rc, _, _ = self.invoke(SimpleNamespace(check=False,version='1.20.0',yes=True))
        self.assertEqual(rc, 3)
        mutate.assert_not_called()

    def test_manager_commands_use_pinned_package_and_exact_environment(self):
        self.assertEqual(upgrade.command(self.install,'1.20.0')[:5], [str(self.python),'-I','-m','pip','--isolated'])
        for manager in ('uv','pipx'):
            install = upgrade.Installation(manager,self.prefix,self.python,self.install.launcher,Path('/tools')/manager)
            cmd = upgrade.command(install,'1.20.0')
            self.assertEqual(cmd[0], '/tools/'+manager)
            self.assertIn('tokenatlas==1.20.0', cmd)
            self.assertNotIn('sudo', cmd)

    def test_snapshot_preserves_environment_and_does_not_include_history(self):
        (self.root/'history.sqlite3').write_text('new writes')
        saved = upgrade.snapshot(self.install)
        self.assertEqual((saved/'environment/bin/python').read_text(), 'python')
        self.assertFalse((saved/'history.sqlite3').exists())
        self.assertEqual((self.root/'history.sqlite3').read_text(), 'new writes')
        self.assertTrue((saved/'recovery.json').is_file())

    def test_failed_install_keeps_backup_and_reports_location(self):
        with patch.object(upgrade,'_execute',return_value=SimpleNamespace(returncode=9)):
            with self.assertRaisesRegex(upgrade.Failed,'backup'):
                upgrade.mutate(self.install,'1.20.0')
        saved = list(self.root.glob('.tokenatlas-backup-*'))
        self.assertEqual(len(saved),1)
        self.assertEqual((saved[0]/'environment/bin/python').read_text(),'python')

    def test_lock_survives_environment_replacement(self):
        with installation_lock(self.prefix):
            self.prefix.rename(self.root/'old')
            self.prefix.mkdir()
            with self.assertRaises(Busy):
                with installation_lock(self.prefix):
                    self.fail('concurrent updater acquired lock')

    def test_same_version_does_not_mutate(self):
        with patch.object(upgrade,'detect',return_value=self.install), patch.object(upgrade,'mutate') as mutate:
            rc, _, _ = self.invoke(SimpleNamespace(check=False,version=upgrade.__version__,yes=True))
        self.assertEqual(rc,0)
        mutate.assert_not_called()

    def test_unpinned_upgrade_never_downgrades(self):
        with patch.object(upgrade,'detect',return_value=self.install), patch.object(upgrade,'latest_version',return_value='1.0.0'), patch.object(upgrade,'mutate') as mutate:
            rc, out, _ = self.invoke(SimpleNamespace(check=False,version=None,yes=True))
        self.assertEqual(rc,0)
        self.assertIn('intentional rollback',out)
        mutate.assert_not_called()

    def test_embedded_data_is_refused_before_install(self):
        (self.prefix/'history.sqlite3').write_text('do not move or overwrite')
        with patch.object(upgrade,'_execute') as execute:
            with self.assertRaises(upgrade.Unsupported):upgrade.mutate(self.install,'1.20.0')
        execute.assert_not_called()
        self.assertEqual((self.prefix/'history.sqlite3').read_text(),'do not move or overwrite')

    def test_custom_database_and_private_state_are_refused(self):
        for name,content in [('custom-data',b'SQLite format 3\0more'),
                             ('custom-data-wal',b'wal'),('custom-data-shm',b'shm'),
                             ('top-prompts.json',b'private'),('outcomes.jsonl',b'outcome'),
                             ('statusline.json',b'{}'),('claude-quota.last',b'quota')]:
            with self.subTest(name=name):
                path=self.prefix/name;path.write_bytes(content)
                with patch.object(upgrade,'_execute') as execute:
                    with self.assertRaises(upgrade.Unsupported):upgrade.mutate(self.install,'1.20.0')
                execute.assert_not_called()
                self.assertEqual(path.read_bytes(),content)
                path.unlink()

    def test_explicit_state_root_does_not_evaluate_home_fallback(self):
        with patch.dict(os.environ,{'XDG_STATE_HOME':str(self.root/'state')}), patch.object(Path,'home',side_effect=RuntimeError('Windows home absent')):
            saved=upgrade.snapshot(self.install)
        self.assertTrue((saved/'recovery.json').is_file())

    def test_exposed_launcher_backup_is_verified_and_restore_is_not_overlay(self):
        exposed=self.root/'tokenatlas';exposed.write_bytes(b'launcher')
        install=upgrade.Installation('pipx',self.prefix,self.python,self.install.launcher,Path('/tools/pipx'),exposed)
        saved=upgrade.snapshot(install)
        import hashlib
        record=json.loads((saved/'recovery.json').read_text())
        self.assertEqual(record['exposed_sha256'],hashlib.sha256((saved/'exposed-launcher').read_bytes()).hexdigest())
        self.assertIn('NEVER overlay', (saved/'README.txt').read_text())

    def test_collect_skips_while_installer_holds_lock(self):
        from tokenatlas import collect
        with installation_lock(self.prefix), patch('sys.prefix',str(self.prefix)), patch.object(collect,'_run') as collect_run:
            self.assertEqual(collect.run(SimpleNamespace()),0)
        collect_run.assert_not_called()

    def test_subprocess_scrubs_wrong_destination_and_checkout(self):
        with patch.dict(os.environ,{'PIP_TARGET':'/wrong','PIP_PREFIX':'/wrong','PIP_CONFIG_FILE':'/wrong','PYTHONPATH':'/wrong',
                                   'UV_EXTRA_INDEX_URL':'https://wrong.invalid','UV_TOOL_DIR':'/owned-tools'}):
            result = upgrade._execute([sys.executable,'-c',
                'import os,json; print(json.dumps({k:os.environ.get(k) for k in ["PIP_TARGET","PIP_PREFIX","PIP_CONFIG_FILE","PYTHONPATH","UV_EXTRA_INDEX_URL","UV_TOOL_DIR","UV_NO_CONFIG"]}))'])
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout),{'PIP_TARGET':None,'PIP_PREFIX':None,'PIP_CONFIG_FILE':os.devnull,'PYTHONPATH':None,
                                                  'UV_EXTRA_INDEX_URL':None,'UV_TOOL_DIR':'/owned-tools','UV_NO_CONFIG':'1'})

    def test_pip_cannot_be_shadowed_by_working_directory(self):
        (self.root/'pip.py').write_text('print("SHADOWED_PIP")')
        installation = upgrade.Installation('venv',self.prefix,Path(sys.executable),self.install.launcher,None)
        with patch.object(upgrade.tempfile,'gettempdir',return_value=str(self.root)):
            result = upgrade._execute(upgrade.command(installation,'1.20.0')[:5]+['--version'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('SHADOWED_PIP',result.stdout)
        self.assertTrue(result.stdout.startswith('pip '),result.stdout)

    def test_timeout_stops_installer_children_before_return(self):
        started=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            upgrade._execute([sys.executable,'-c',
                'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(8)"]); time.sleep(10)'],timeout=.5)
        self.assertLess(time.monotonic()-started,6)


if __name__ == '__main__':
    unittest.main()
