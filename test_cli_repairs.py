"""Focused regressions for the provisional issue-100 CLI repairs."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_plans_cli import row
from tokenatlas import __main__ as cli
from tokenatlas import collect
from tokenatlas.history import History, _encode


class CollectRepairTests(unittest.TestCase):
    def test_top_no_data_is_tolerated_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.sqlite3"
            store = db.with_name("top-prompts.json")
            store.write_text('{"version":2,"k":5,"by":"cost","entries":[]}\n')
            store.chmod(0o600)
            args = SimpleNamespace(
                db=db,
                no_report=True,
                lang="auto",
                remote=[],
                remote_sync=None,
                sync_timeout=1,
            )
            steps = []
            with mock.patch.object(cli, "refresh_all", return_value={"status": "ok"}), \
                    mock.patch.object(collect, "_hosts", return_value=""), \
                    mock.patch.object(collect, "_top_up_to_date", return_value=False), \
                    mock.patch.object(collect, "_quiet", return_value=4), \
                    mock.patch.object(collect.signal, "signal"):
                code = collect._run(args, steps)
            self.assertEqual(code, 0)
            self.assertEqual(
                steps,
                [
                    {"name": "refresh", "exit_code": 0, "ok": True},
                    {"name": "top", "exit_code": 4, "ok": True, "skipped": "no_data"},
                ],
            )

    def test_top_other_failure_is_not_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.sqlite3"
            store = db.with_name("top-prompts.json")
            store.write_text('{"version":2,"k":5,"by":"cost","entries":[]}\n')
            store.chmod(0o600)
            args = SimpleNamespace(
                db=db,
                no_report=True,
                lang="auto",
                remote=[],
                remote_sync=None,
                sync_timeout=1,
            )
            steps = []
            with mock.patch.object(cli, "refresh_all", return_value={"status": "ok"}), \
                    mock.patch.object(collect, "_hosts", return_value=""), \
                    mock.patch.object(collect, "_top_up_to_date", return_value=False), \
                    mock.patch.object(collect, "_quiet", return_value=1), \
                    mock.patch.object(collect.signal, "signal"):
                code = collect._run(args, steps)
            self.assertEqual(code, 1)
            self.assertEqual(
                steps,
                [
                    {"name": "refresh", "exit_code": 0, "ok": True},
                    {"name": "top", "exit_code": 1, "ok": False},
                ],
            )


class RefreshRepairTests(unittest.TestCase):
    def test_explicit_missing_root_is_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.sqlite3"
            missing = root / "missing-claude-root"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "USERPROFILE": str(root),
                    "XDG_STATE_HOME": str(root / "state"),
                },
                clear=False,
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(
                    [
                        "--db",
                        str(db),
                        "refresh",
                        "--harness",
                        "claude",
                        "--root",
                        str(missing),
                        "--json",
                    ]
                )
            self.assertEqual(code, 3, err.getvalue())
            self.assertEqual(out.getvalue(), "")
            payload = json.loads(err.getvalue())
            self.assertEqual(payload["error"]["code"], "not_found")


class WrongTypeRootRepairTests(unittest.TestCase):
    def test_file_instead_of_source_directory_is_internal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "not-a-directory"
            source.write_text("invalid source root")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--db", str(root / "history.sqlite3"), "refresh", "--harness", "claude", "--root", str(source), "--json"])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertEqual(json.loads(err.getvalue())["error"]["code"], "internal")


class ProgressRepairTests(unittest.TestCase):
    def test_forced_progress_stays_on_stderr_for_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.sqlite3"
            with History(db) as history:
                history.connection.execute("BEGIN")
                history._strings, history._values = {}, {}
                history._insert(
                    history.connection,
                    _encode(row("r", "2026-09-03T10:00:00+00:00", "t")),
                )
                history._bump(history.connection)
                history.connection.commit()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "USERPROFILE": str(root),
                    "XDG_STATE_HOME": str(root / "state"),
                    "TOKENATLAS_PROGRESS": "1",
                },
                clear=False,
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--db", str(db), "top", "--json"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["schema"], "tokenatlas.top/1")
            self.assertIn("Read history ...", err.getvalue())


if __name__ == "__main__":
    unittest.main()


import test_collect


class CollectJSONStreamsTests(test_collect.CollectBase):
    @unittest.skipIf(os.name == 'nt', 'remote sync uses bash')
    def test_remote_failure_both_streams_are_inside_json_diagnostics(self):
        self.sync.write_text('#!/usr/bin/env bash\necho remote-stdout\necho remote-stderr >&2\nexit 3\n')
        proc = self.collect('--no-report', '--remote', 'pi:synthetic', '--remote-sync', str(self.sync), '--json')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, '')
        data = json.loads(proc.stderr)
        self.assertEqual(data['error']['code'], 'internal')
        self.assertIn('remote-stdout', data['error']['details']['diagnostics'])
        self.assertIn('remote-stderr', data['error']['details']['diagnostics'])


class UpgradeJSONPromptTests(unittest.TestCase):
    def test_json_requires_explicit_yes_even_with_tty(self):
        from tokenatlas import upgrade
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            install = upgrade.Installation('venv', prefix, prefix/'bin/python', prefix/'bin/tokenatlas', None)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(upgrade, 'detect', return_value=install), mock.patch('sys.stdin.isatty', return_value=True), mock.patch('builtins.input', return_value='n') as prompt, mock.patch.object(upgrade, 'mutate') as mutate, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(['upgrade', '--version', '99.0.0', '--json'])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('--yes', json.loads(err.getvalue())['error']['message'])
            prompt.assert_not_called()
            mutate.assert_not_called()
