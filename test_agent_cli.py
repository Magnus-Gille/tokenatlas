"""Agent-facing CLI contract, using only synthetic history and mocked side effects."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tokenatlas import __main__ as cli
from tokenatlas.history import History, _encode
from test_plans_cli import row


class AgentCli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'history.sqlite3'
        self.env = mock.patch.dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root),
                                   XDG_STATE_HOME=str(self.root / 'state'), TOKENATLAS_NO_BROWSER='1')
        self.env.start()
        self.addCleanup(self.env.stop)
        with History(self.db) as h:
            h.connection.execute('BEGIN')
            h._strings, h._values = {}, {}
            h._insert(h.connection, _encode(row('r', '2026-09-03T10:00:00+00:00', 't',model='gpt-5.5',plan='pro')))
            h._bump(h.connection)
            h.connection.commit()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try: code = cli.main(['--db', str(self.db), *args])
            except SystemExit as exc: code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_help_examples(self):
        commands = ['refresh', 'open', 'show', 'snapshot', 'import', 'report', 'session', 'rate',
                    'top', 'insights', 'overhead', 'collect', 'schedule', 'upgrade', 'statusline',
                    'quota', 'quota show', 'quota set', 'quota calibrate', 'quota forget',
                    'plan', 'plan set', 'plan remove', 'plan show', 'profile', 'profile provider',
                    'profile provider add', 'profile provider remove', 'profile provider show', 'doctor']
        for command in commands:
            with self.subTest(command=command):
                code, out, err = self.run_cli(*command.split(), '--help')
                self.assertEqual(code, 0, err)
                self.assertIn('Examples:', out)
        self.assertIn('docs/agents.md', self.run_cli('--help')[1])

    def test_json_success(self):
        snapshot = self.root / 'copy.sqlite3'
        cases = [('doctor',), ('refresh', '--harness', 'codex', '--root', str(self.root)),
                 ('report',), ('report', '--html', str(self.root / 'r.html')),
                 ('report','--html',str(self.root/'r.html'),'--if-changed'),
                 ('open','--no-refresh','--no-browser','--html',str(self.root/'r.html')),
                 ('show','--print-path','--html',str(self.root/'r.html')),
                 ('top',), ('insights',), ('session', 'codex:s'), ('rate', 'codex:s'),
                 ('snapshot', str(snapshot)), ('import', str(snapshot), '--label', 'demo'),
                 ('overhead',), ('plan', 'show'), ('profile', 'provider', 'show'),
                 ('quota', 'show'), ('schedule', '--status')]
        for args in cases:
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args, '--json')
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)['schema'], f'tokenatlas.{args[0]}/1')

    def test_error_contract(self):
        cases = [(('session', 'nonexistent'), 3, 'not_found'),
                 (('top', '--start', 'garbage'), 2, 'usage'),
                 (('top', '--start', '2099-01-01T00:00Z'), 4, 'no_data'),
                 (('report', '--start', '2099-01-01T00:00Z'), 4, 'no_data'),
                 (('insights', '--start', '2099-01-01T00:00Z'), 4, 'no_data'),
                 (('top', '--nonsense'), 2, 'usage'),
                 (('show', '--html', str(self.root / 'missing.html')), 3, 'not_found'),
                 (('import', str(self.root / 'missing.sqlite3'), '--label', 'x'), 3, 'not_found')]
        for args, expected, error in cases:
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args, '--json')
                self.assertEqual(code, expected, err)
                self.assertEqual(out, '')
                data = json.loads(err)
                self.assertFalse(data['ok'])
                self.assertEqual(data['error']['code'], error)
                self.assertTrue(data['error']['hint'])
                self.assertIn('schema', data)
        self.assertIn('ISO 8601', self.run_cli('top', '--start', 'garbage', '--json')[2])
        code,out,err=self.run_cli('unknown-command','--json')
        self.assertEqual(code,2)
        data=json.loads(err)
        self.assertEqual(data['schema'],'tokenatlas.cli/1')
        self.assertEqual(data['error']['hint'],'See tokenatlas --help.')

    def test_date_only_and_local_iso_are_documented_inputs(self):
        for stamp in ('2026-09-03', '2026-09-03T09:00'):
            code, out, err = self.run_cli('top', '--start', stamp, '--json')
            self.assertEqual(code, 0, err)

    def test_no_browser_paths(self):
        path = self.root / 'r.html'
        with mock.patch.object(cli.webbrowser, 'open', side_effect=AssertionError('browser')):
            for extra in (('--no-browser',), ()):
                code, out, err = self.run_cli('open', '--no-refresh', '--html', str(path), *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), str(path))
            for extra in (('--print-path',), ()):
                code, out, err = self.run_cli('show', '--html', str(path), *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), str(path))
            code, out, err = self.run_cli('show', '--html', str(path), '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['schema'], 'tokenatlas.show/1')

    def test_rate_records_a_discovered_thread(self):
        child=row('child','2026-09-03T10:01:00+00:00','child-turn',session='child')
        child.update(parent_session='s',thread_kind='subagent',agent='worker')
        with History(self.db) as h:
            h.connection.execute('BEGIN');h._strings,h._values={},{}
            h._insert(h.connection,_encode(child));h._bump(h.connection);h.connection.commit()
        code,out,err=self.run_cli('rate','codex:s','--json')
        self.assertEqual(code,0,err)
        key=json.loads(out)['threads'][0]['key']
        code,out,err=self.run_cli('rate','codex:s','--unit','demo','--thread',key,'--outcome','pass','--json')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['schema'],'tokenatlas.rate/1')
        saved=json.loads(self.db.with_name('outcomes.jsonl').read_text())
        self.assertEqual(saved['unit'],'demo')
        self.assertEqual(saved['outcome'],'pass')
        code,out,err=self.run_cli('rate','codex:s','--unit','demo','--thread','codex:missing','--outcome','pass','--json')
        self.assertEqual(code,3,err)
        self.assertEqual(out,'')
        self.assertEqual(len(self.db.with_name('outcomes.jsonl').read_text().splitlines()),1)

    def test_collect_step_results_and_failure_details(self):
        from tokenatlas import collect
        for status,expected in (('ok',0),('partial',1)):
            with self.subTest(status=status), mock.patch.object(cli,'refresh_all',return_value={'status':status}), \
                    mock.patch.object(collect,'_hosts',return_value=''), mock.patch.object(collect.signal,'signal'):
                code,out,err=self.run_cli('collect','--no-report','--json')
                self.assertEqual(code,expected,err)
                if code:
                    self.assertEqual(out,'')
                    data=json.loads(err)['error']['details']
                else:
                    data=json.loads(out)
                    self.assertEqual(data['schema'],'tokenatlas.collect/1')
                self.assertEqual(data['steps'],[{'name':'refresh','exit_code':expected,'ok':not expected}])
                self.assertIn('collect:',data['diagnostics'])

    def test_quota_json_mutations(self):
        for args in [('calibrate','--harness','codex','--window','5h','--used','52%','--plan','pro','--at','2026-09-03T10:30Z'),
                     ('set','--harness','claude','--window','5h','--budget-usd','100'),
                     ('forget','--harness','claude')]:
            code,out,err=self.run_cli('quota',*args,'--json')
            self.assertEqual(code,0,err)
            self.assertEqual(json.loads(out)['schema'],'tokenatlas.quota/1')

    def test_nested_json_can_precede_the_leaf(self):
        code,out,err=self.run_cli('quota','--json','show')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['schema'],'tokenatlas.quota/1')

    def test_no_data_does_not_write_a_report(self):
        path=self.root/'empty.html'
        code,out,err=self.run_cli('report','--start','2099-01-01','--html',str(path),'--json')
        self.assertEqual(code,4,err)
        self.assertFalse(path.exists())

    def test_upgrade_check_json_without_network(self):
        from tokenatlas import upgrade
        from tokenatlas import __version__
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(upgrade,'detect',side_effect=upgrade.Unsupported('synthetic install')), \
                mock.patch.object(upgrade,'latest_version',return_value=__version__), \
                contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=cli.main(['upgrade','--check','--json'])
            except SystemExit as exc:code=exc.code
        self.assertEqual(code,0,err.getvalue())
        data=json.loads(out.getvalue())
        self.assertEqual(data['schema'],'tokenatlas.upgrade/1')
        self.assertEqual(data['current'],__version__)
        self.assertEqual(data['status'],'checked')

    def test_internal_error(self):
        with mock.patch.object(History, 'doctor', side_effect=RuntimeError('synthetic failure')):
            code, out, err = self.run_cli('doctor', '--json')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(json.loads(err)['error']['code'], 'internal')


if __name__ == '__main__':
    unittest.main()
