"""Scheduler plans use fake homes and fake scheduler commands only."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenatlas import schedule


class ScheduleTest(unittest.TestCase):
    def test_windows_xml_ownership_and_failed_delete(self):
        import xml.etree.ElementTree as ET
        script, log = schedule._windows_paths()
        script.parent.mkdir(parents=True)
        script.write_text(schedule._windows_script([], log))
        root = ET.Element('Task', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
        action = ET.SubElement(ET.SubElement(root, 'Actions'), 'Exec')
        ET.SubElement(action, 'Command').text = schedule._windows_action(script)[0]
        ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline([str(script)])
        xml = ET.tostring(root, encoding='unicode')
        def fake(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, xml, '') if '/Query' in command else subprocess.CompletedProcess(command, 1, '', 'denied')
        with patch.object(schedule, '_run', side_effect=fake):
            self.assertEqual(schedule._task_state(script), 'owned')
            with self.assertRaises(schedule.ScheduleError):
                schedule._remove('windows', False)
        self.assertTrue(script.exists())

    def test_windows_minute_limit_checked(self):
        with patch.object(schedule, '_task_state', return_value='absent'):
            with self.assertRaises(schedule.ScheduleError):
                schedule._install(1440 * 60, [], 'schtasks', 'windows', True)

    def test_crontab_error_does_not_mean_empty(self):
        with patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 2, '', '')):
            with self.assertRaises(schedule.ScheduleError):
                schedule._crontab()

    def test_loaded_foreign_launchd_service_is_never_stopped(self):
        with patch.object(schedule, '_launch_state', return_value='foreign'), patch.object(schedule, '_run') as run:
            with self.assertRaises(schedule.ScheduleError):
                schedule._install(1800, [], 'launchd', 'darwin', False)
        run.assert_not_called()

    def test_systemd_failed_disable_keeps_both_units(self):
        service, timer, _ = schedule._systemd_paths()
        service.parent.mkdir(parents=True)
        service.write_text('# Managed by tokenatlas schedule\n')
        timer.write_text('# Managed by tokenatlas schedule\n')
        with patch.object(schedule, '_check_systemd_paths'), patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaises(schedule.ScheduleError):
                schedule._remove('linux', False)
        self.assertTrue(service.exists())
        self.assertTrue(timer.exists())

    def test_failed_remove_preserves_files(self):
        path, _ = schedule._mac_paths()
        path.parent.mkdir(parents=True)
        path.write_bytes(schedule._plist(1800, [], self.root / 'log'))
        with patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaises(schedule.ScheduleError):
                schedule._remove('darwin', False)
        self.assertTrue(path.exists())

    def test_systemd_environment_dollar_is_literal_but_exec_dollar_is_escaped(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'cash$HOME%')}):
            service, _ = schedule._units(1800, [], self.root / 'log', self.root / 'cash$HOME.sqlite3')
        env = next(line for line in service.splitlines() if 'CODEX_HOME=' in line)
        self.assertIn('cash$HOME%%', env)
        self.assertNotIn('$$', env)
        self.assertIn('cash$$HOME.sqlite3', service)

    def test_cron_environment_percent_is_escaped_and_log_directory_created(self):
        log = self.root / 'new-log-dir' / 'collect.log'
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'percent%dir')}):
            line = schedule._cron_line(1800, [], log)
        self.assertIn(r'percent\%dir', line)
        with patch.object(schedule, '_crontab', return_value=('', False)), patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            schedule._cron_install(1800, [], log, False)
        self.assertTrue(log.parent.is_dir())

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows privileges")
    def test_virtualenv_interpreter_path_not_resolved(self):
        real = self.root / 'python-real'; real.write_text('')
        link = self.root / 'venv' / 'python'; link.parent.mkdir(); link.symlink_to(real)
        with patch.object(schedule.sys, 'argv', ['-m']), patch.object(schedule.sys, 'executable', str(link)):
            self.assertEqual(schedule._executable()[0], str(link))

    def test_windows_launcher_executes_literal_argv_without_shell(self):
        command = [str(self.root / 'python'), '-m', 'tokenatlas']
        db = self.root / 'cash%&!^$.sqlite3'
        with patch.object(schedule, '_executable', return_value=command):
            code = schedule._windows_script([], self.root / 'collect.log', db)
        calls = []
        with patch('subprocess.run', side_effect=lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0)), self.assertRaises(SystemExit) as done:
            exec(compile(code, '<generated-runner>', 'exec'), {'__name__': '__main__'})
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(calls[0][0], command + ['--db', str(db), 'collect'])
        self.assertFalse(calls[0][1].get('shell', False))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": "/usr/bin:/bin", "LOCALAPPDATA":str(self.home / "AppData"), "XDG_CONFIG_HOME":str(self.home / ".config"), "XDG_STATE_HOME":str(self.home / ".local/state"), "TOKENATLAS_DB":"", **{key:"" for key in schedule.HARNESS_VARS}}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        owner = patch.object(schedule, '_launch_state', side_effect=lambda path: 'owned' if path.exists() and schedule._owned(path) else 'absent')
        owner.start(); self.addCleanup(owner.stop)

    def args(self, **values):
        defaults = dict(every="30m", remote=[], dry_run=False, status=False, remove=False)
        defaults.update(values)
        return type("Args", (), defaults)()

    def test_backend_switch_refuses_duplicate_owned_jobs(self):
        with patch.object(schedule, '_platform', return_value='linux'), patch.object(schedule, '_backend', return_value='systemd'), patch.object(schedule, '_crontab', return_value=('*/30 * * * * tokenatlas collect # tokenatlas schedule\n', True)), patch.object(schedule, '_install') as install:
            with self.assertRaisesRegex(schedule.ScheduleError, 'remove'):
                schedule.run(self.args(dry_run=True))
        install.assert_not_called()
        service, timer, _ = schedule._systemd_paths()
        service.parent.mkdir(parents=True)
        service.write_text('# Managed by tokenatlas schedule\n')
        timer.write_text('# Managed by tokenatlas schedule\n')
        with patch.object(schedule, '_platform', return_value='linux'), patch.object(schedule, '_backend', return_value='cron'), patch.object(schedule, '_install') as install:
            with self.assertRaisesRegex(schedule.ScheduleError, 'remove'):
                schedule.run(self.args(dry_run=True))
        install.assert_not_called()

    def test_systemd_log_path_is_unquoted_and_literal(self):
        service, _ = schedule._units(1800, [], self.home / 'måns % logs' / 'collect.log')
        self.assertIn('StandardOutput=append:' + str(self.home / 'måns %% logs' / 'collect.log'), service)
        self.assertNotIn('StandardOutput="', service)

    def test_cron_status_recovers_the_complete_argv(self):
        with patch.object(schedule, '_executable', return_value=[str(self.home / 'bin' / 'python'), '-m', 'tokenatlas']):
            expected = schedule._argv(['pi:host'], self.home / 'database file')
            line = schedule._cron_line(1800, ['pi:host'], self.home / 'log', self.home / 'database file')
        self.assertEqual(schedule._shell_target(line), expected)

    def test_cron_refuses_truncated_or_ambiguous_command(self):
        with patch.object(schedule, '_harness_environment', return_value={'PATH':'/' + 'a'*1000}):
            with self.assertRaisesRegex(schedule.ScheduleError, '999'):
                schedule._cron_line(1800, [], self.home / 'log')
        with self.assertRaisesRegex(schedule.ScheduleError, 'backslash'):
            schedule._cron_line(1800, [], self.home / r'back\%slash')

    def test_cron_preserves_nonstandard_line_endings_without_echoing_them(self):
        current = '1 * * * * echo unrelated\r\n# arbitrary\u2028text\n'
        calls = []
        with patch.object(schedule, '_crontab', return_value=(current, True)), patch.object(schedule, '_run', side_effect=lambda command, **kw: calls.append(kw.get('input_text')) or subprocess.CompletedProcess(command, 0, '', '')):
            installed = schedule._cron_install(1800, [], self.home / 'log', False)
            planned = schedule._cron_install(1800, [], self.home / 'log', True)
        self.assertTrue(calls[0].startswith(current))
        self.assertNotIn('stdin', installed)
        self.assertEqual(planned['stdin'], calls[0])

    def test_removal_reports_unreadable_schedulers(self):
        with patch.object(schedule, '_crontab', side_effect=schedule.ScheduleError('denied')):
            with self.assertRaisesRegex(schedule.ScheduleError, 'denied'):
                schedule._remove('linux', False)
        script, log = schedule._windows_paths(); script.parent.mkdir(parents=True)
        script.write_text(schedule._windows_script([], log))
        with patch.object(schedule, '_task_state', return_value='error'):
            with self.assertRaisesRegex(schedule.ScheduleError, 'ownership'):
                schedule._remove('windows', False)
        self.assertTrue(script.exists())

    def test_status_query_failure_is_unknown_with_diagnostic(self):
        with patch.object(schedule, '_crontab', side_effect=schedule.ScheduleError('denied')):
            result = schedule.status('linux')
        self.assertIsNone(result['installed'])
        self.assertIn('denied', result['errors'])

    def test_default_db_resolves_relative_state_like_cli_without_creating_it(self):
        with patch.dict(os.environ, {'XDG_STATE_HOME':'relative-state'}):
            self.assertEqual(schedule._resolved_db(), (Path('relative-state')/'tokenatlas/history.sqlite3').absolute())

    def test_backend_check_does_not_treat_cron_read_errors_as_absence(self):
        with patch.object(schedule, '_platform', return_value='linux'), patch.object(schedule, '_backend', return_value='systemd'), patch.object(schedule, '_crontab', side_effect=schedule.ScheduleError('denied')), patch.object(schedule, '_install') as install:
            with self.assertRaisesRegex(schedule.ScheduleError, 'denied'):
                schedule.run(self.args(dry_run=True))
        install.assert_not_called()

    def test_remove_crlf_owned_cron_keeps_other_bytes_on_both_unix_platforms(self):
        other='1 * * * * echo untouched\r\n'
        current=other+'*/30 * * * * tokenatlas collect # tokenatlas schedule\r\n'
        for platform in ('linux','darwin'):
            writes=[]
            with patch.object(schedule, '_crontab', return_value=(current, True)), patch.object(schedule, '_run', side_effect=lambda cmd, **kw: writes.append(kw.get('input_text')) or subprocess.CompletedProcess(cmd,0,'','')):
                result=schedule._remove(platform,False)
            self.assertEqual(writes,[other])
            self.assertIn('crontab',result['removed'])

    def test_fresh_systemd_rollback_disables_before_removing_units(self):
        service,timer,_=schedule._systemd_paths()
        calls=[]
        def command(argv, **kwargs):
            calls.append(argv)
            if 'disable' in argv:
                self.assertTrue(timer.exists(), 'disable requires the installed unit to exist')
            return subprocess.CompletedProcess(argv,1 if 'restart' in argv else 0,'','restart failure')
        with patch.object(schedule,'_check_systemd_paths'),patch.object(schedule,'_run',side_effect=command):
            with self.assertRaisesRegex(schedule.ScheduleError,'restart'):
                schedule._install(1800,[],'systemd','linux',False)
        self.assertTrue(any('disable' in c for c in calls))
        self.assertFalse(service.exists());self.assertFalse(timer.exists())

    def test_parse_and_reject_remote(self):
        self.assertEqual(schedule.parse_every("30m"), 1800)
        self.assertEqual(schedule.parse_every("1h"), 3600)
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_every("0m")
        with self.assertRaises(schedule.ScheduleError):
            schedule._remote_pairs(["pi:host;touch"])

    def test_dry_run_has_no_files_and_contains_absolute_argv(self):
        with patch.object(schedule, "_platform", return_value="darwin"), patch.object(schedule, "_executable", return_value=[str(self.root / "bin dir" / "tokenatlas")]):
            result = schedule._install(1800, ["pi:host.example"], "launchd", "darwin", True)
        self.assertEqual(result["writes"], [])
        self.assertIn("RunAtLoad", result["content"])
        self.assertIn(str(self.root / "bin dir" / "tokenatlas"), result["content"])
        self.assertFalse((self.home / "Library").exists())

    def test_launchd_install_status_and_owned_remove(self):
        calls = []
        fake_run = lambda command, **kwargs: (calls.append(command) or subprocess.CompletedProcess(command, 0, "", ""))
        with patch.object(schedule, "_run", side_effect=fake_run), patch.object(schedule, "_executable", return_value=[str(self.root / "bin" / "tokenatlas")]), patch.object(schedule, "_platform", return_value="darwin"):
            installed = schedule._install(1800, ["pi:host.example"], "launchd", "darwin", False)
            current = schedule.status("darwin")
            removed = schedule._remove("darwin", False)
        self.assertEqual(installed["backend"], "launchd")
        self.assertEqual(current["schedules"][0]["argv"][2:], ["--remote", "pi:host.example"])
        self.assertEqual(removed["removed"], [str(self.home / "Library/LaunchAgents/com.tokenatlas.collect.plist")])
        self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in calls))
        self.assertFalse((self.home / "Library/LaunchAgents/com.tokenatlas.collect.plist").exists())

    def test_cron_replaces_only_owned_line(self):
        old = "17 * * * * echo unrelated\n*/15 * * * * tokenatlas collect # hand written\n"
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs.get("input_text")))
            if command[-1] == "-l":
                return subprocess.CompletedProcess(command, 0, old, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(schedule, "_run", side_effect=fake_run), patch.object(schedule.shutil, "which", return_value="/fake/crontab"), patch.object(schedule, "_executable", return_value=[str(self.root / "tokenatlas")]):
            result = schedule._install(1800, ["tag:host.example"], "cron", "linux", False)
        entry = [text for command, text in calls if command[-1] == "-" and text][0]
        self.assertIn("echo unrelated", entry)
        self.assertIn("# hand written", entry)
        self.assertEqual(entry.count("# tokenatlas schedule"), 1)
        self.assertTrue(result["warnings"])

    def test_systemd_and_windows_are_rendered_without_shell_interpolation(self):
        with patch.object(schedule, "_executable", return_value=[str(self.root / "bin dir" / "tokenatlas")]):
            service, timer = schedule._units(1800, ["tag:host.example"], self.root / "logs" / "collect.log")
            script = schedule._windows_script(["tag:host.example"], self.root / "logs" / "collect.log")
        self.assertIn("ExecStart=", service)
        self.assertIn('ExecStart="', service)
        self.assertIn("OnUnitActiveSec=1800s", timer)
        self.assertIn("Managed by tokenatlas schedule", script)
        self.assertIn("collect.log", script)

    def test_unowned_scheduler_file_is_preserved(self):
        path, _ = schedule._mac_paths()
        path.parent.mkdir(parents=True)
        path.write_text("user-owned plist")
        with self.assertRaises(schedule.ScheduleError):
            schedule._install(1800, [], "launchd", "darwin", False)
        self.assertEqual(path.read_text(), "user-owned plist")

    def test_db_and_harness_environment_are_carried_into_every_backend(self):
        db = self.root / "state with spaces" / "history.sqlite3"
        env = {"CLAUDE_CONFIG_DIR": str(self.root / "claude dir"), "CODEX_HOME": str(self.root / "codex%$dir")}
        with patch.dict(os.environ, env, clear=False), patch.object(schedule, "_executable", return_value=[str(self.root / "bin dir" / "tokenatlas")]):
            plist = schedule._plist(1800, [], self.root / "log", db)
            service, _ = schedule._units(1800, [], self.root / "log %$", db)
            cron = schedule._cron_line(1800, [], self.root / "log%", db)
        self.assertIn(str(db), plist.decode())
        self.assertIn('Environment="CLAUDE_CONFIG_DIR=', service)
        self.assertIn('Environment="CODEX_HOME=', service)
        self.assertNotIn("$$", service)  # Environment/Output paths do not perform dollar expansion
        self.assertIn("%%", service)
        self.assertIn("--db", cron)
        self.assertIn(r"\%", cron)

    def test_reinstall_updates_owned_launchd_interval_and_restores_on_failure(self):
        outcomes = [0, 0, 1, 0]  # first bootstrap; bootout; failed update; successful restoration
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, outcomes.pop(0) if outcomes else 0, "", "failed")
        with patch.object(schedule, "_run", side_effect=fake_run):
            schedule._install(1800, [], "launchd", "darwin", False)
            path, _ = schedule._mac_paths()
            old = path.read_bytes()
            with self.assertRaises(schedule.ScheduleError):
                schedule._install(3600, [], "launchd", "darwin", False)
            self.assertEqual(path.read_bytes(), old)

    def test_windows_task_must_point_at_our_wrapper_before_force_replace_or_remove(self):
        script, _ = schedule._windows_paths()
        script.parent.mkdir(parents=True)
        script.write_text("rem Managed by tokenatlas schedule\n")

        def foreign(command, **kwargs):
            if command[:2] == ["schtasks", "/Query"]:
                return subprocess.CompletedProcess(command, 0, "Task To Run: C:\\other\\job.cmd\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(schedule, "_run", side_effect=foreign):
            with self.assertRaises(schedule.ScheduleError):
                schedule._install(1800, [], "schtasks", "windows", False)
            removed = schedule._remove("windows", False)
        self.assertEqual(removed["removed"], [])
        self.assertTrue(script.exists())

    def test_systemd_probe_controls_dry_run_and_install_backend(self):
        with patch.object(schedule.shutil, "which", return_value="/fake/systemctl"), patch.object(schedule, "_run", return_value=subprocess.CompletedProcess([], 1, "", "not running")):
            self.assertEqual(schedule._backend("linux", True), "cron")
            self.assertEqual(schedule._backend("linux", False), "cron")

    def test_status_reports_loaded_and_enabled_state(self):
        service, timer, _ = schedule._systemd_paths()
        service.parent.mkdir(parents=True)
        service.write_text("# Managed by tokenatlas schedule\nExecStart=/bin/tokenatlas collect\n")
        timer.write_text("# Managed by tokenatlas schedule\n")

        def state(command, **kwargs):
            output = "inactive\n" if "is-active" in command else "disabled\n"
            return subprocess.CompletedProcess(command, 1, output, "")

        with patch.object(schedule, "_run", side_effect=state):
            result = schedule.status("linux")
        self.assertEqual(result["schedules"][0]["enabled"], "disabled")
        self.assertEqual(result["schedules"][0]["active"], "inactive")


if __name__ == "__main__":
    unittest.main()
