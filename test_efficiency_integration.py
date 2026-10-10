"""Cross-surface and packaging regressions for the token-efficiency contract."""
import json
import random
import shutil
import subprocess
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tokenatlas import efficiency, report
from test_insights import ob, TABLE
from test_fresh_report import Base

ROOT = Path(__file__).resolve().parent


def records():
    parent = ob('parent', ts='2026-10-01T22:30:00+00:00', turn='private-turn', fresh=50, read=200, out=10)
    child = ob('child', ts='2026-10-02T10:00:00+00:00', session='child-session', kind='subagent', turn='child', fresh=20, read=150, out=30)
    child['parent_session'] = 's'
    auto = ob('auto', ts='2026-10-02T11:00:00+00:00', model='codex-auto-review', kind='subagent', fresh=4, out=2)
    unlinked = ob('unlinked', ts='2026-10-02T12:00:00+00:00', fresh=500, out=50, project='/private/another-worktree')
    missing = ob('missing', ts='2026-10-02T13:00:00+00:00', turn='t2', fresh=None, out=5)
    missing['complete'] = False
    synthetic = ob('synthetic', ts='2026-10-02T14:00:00+00:00', fresh=999999, turn='t3')
    synthetic['id_synthetic'] = True
    previous = ob('previous', ts='2026-10-01T10:00:00+00:00', turn='previous', fresh=100, out=20)
    future = ob('future', ts='2026-10-03T14:00:00+00:00', turn='future', fresh=999999)
    return [parent, child, auto, unlinked, missing, synthetic, previous, future]


class SurfaceParity(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'JavaScript parity needs Node (required in browser CI)')
    def test_generated_edge_populations_have_identical_evidence(self):
        rng=random.Random(157)
        cases=[]
        for case in range(24):
            rows=[]
            for i in range(case*3):
                rows.append(dict(ts=(datetime(2026,10,24,tzinfo=timezone.utc)+timedelta(hours=rng.randrange(72),microseconds=rng.choice([0,999,123456]))).isoformat(),
                    tokens={key:rng.choice([None,0,1,100,200000]) for key in ['fresh_input','cache_read','cache_write','output']},
                    prompt=rng.choice([None,0,2,10,1000]),project_id=rng.choice([None,'Project 002','Project 010','Project 1000','private-name']),
                    complete=rng.choice([True,False]),id_synthetic=rng.choice([True,False]),
                    thread_kind=rng.choice(['main','subagent','automation','unknown']),turn_confidence=rng.choice(['derived','observed',None]),
                    efficiency_auto_review=rng.choice([True,False]),efficiency_rolled_up=rng.choice([True,False])))
            options=dict(start='2026-10-25T00:00:00+02:00',end='2026-10-26T00:00:00+01:00',
                snapshot=rng.choice(['2026-10-25T12:00:00Z','2026-10-27T00:00:00Z']),timezone='Europe/Stockholm',
                top_n=rng.choice([1,2,50]),context_threshold=rng.choice([1,200000]),filters={'search':True,'zoom':False,'model':'private-model'})
            cases.append(dict(rows=rows,options=options))
        script="const e=require('./tokenatlas/efficiency.js'),c=JSON.parse(require('fs').readFileSync(0,'utf8')); console.log(JSON.stringify(c.map(x=>e.facts(x.rows,x.options))));"
        result=subprocess.run(['node','-e',script],cwd=ROOT,input=json.dumps(cases),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for index,(case,actual) in enumerate(zip(cases,json.loads(result.stdout))):
            with self.subTest(case=index):self.assertEqual(actual,efficiency.facts(case['rows'],**case['options']))

    def test_record_adapter_derives_roles_from_source_metadata(self):
        rows = efficiency.rows_from_records(records())
        self.assertEqual(sum(r['efficiency_auto_review'] for r in rows), 1)
        self.assertGreaterEqual(sum(r['efficiency_rolled_up'] for r in rows), 1)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript parity needs Node (required in browser CI)')
    def test_python_and_browser_engine_match(self):
        rows = efficiency.rows_from_records(records(), timezone='Europe/Stockholm')
        options = dict(start='2026-10-02T00:00:00+02:00', end='2026-10-03T00:00:00+02:00',
                       snapshot='2026-10-02T15:00:00Z', timezone='Europe/Stockholm', top_n=2, context_threshold=200, contributor_limit=5)
        script = "const fs=require('fs'), e=require('./tokenatlas/efficiency.js'), p=JSON.parse(fs.readFileSync(0,'utf8')); const b=e.facts(p.rows,p.options); console.log(JSON.stringify({bundle:b,scenario:e.scenario(b,'subagent_input',20)}));"
        p = subprocess.run(['node', '-e', script], cwd=ROOT, input=json.dumps(dict(rows=rows, options=options)), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        actual = json.loads(p.stdout)
        expected = efficiency.facts(rows, **options)
        self.assertEqual(actual['bundle'], expected)
        self.assertEqual(actual['scenario'], efficiency.scenario(expected, 'subagent_input', 20))
        encoded = json.dumps(actual)
        for private in ('private-turn', '/private/', 'secretapp', 'child-session', 'another-worktree'):
            self.assertNotIn(private, encoded)

    def test_report_metadata_preserves_role_before_redaction(self):
        data = report.build_report(records(), {}, table=TABLE, redact=True,
                                   now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        meta = data['analytics_metadata']['efficiency']
        self.assertEqual(sum(meta['auto_review']), 1)
        self.assertGreaterEqual(sum(meta['rolled_up']), 1)
        html = report.render_report(data)
        self.assertNotIn('__EFFICIENCY_JS__', html)
        self.assertTrue('TokenEfficiency' in html, 'packaged report must embed the efficiency engine')


class EfficiencyCLI(Base):
    def test_additive_json_and_explicit_scenario(self):
        self.claude()
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        code, out, err = self.run_cli('insights', '--json', '--top-turns', '2', '--context-threshold', '100',
                                      '--scenario-population', 'subagent_input', '--reduction-percent', '20')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn('facts', data)
        ev = data['token_efficiency']
        self.assertEqual(ev['schema_version'], 1)
        self.assertEqual(ev['settings']['top_n'], 2)
        self.assertEqual(ev['scenario']['percent'], 20)
        self.assertNotIn(str(self.home), json.dumps(ev))

    def test_unchanged_history_has_stable_evidence_snapshot(self):
        from unittest.mock import patch
        from tokenatlas import __main__ as cli
        self.claude()
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        before = self.run_cli('insights', '--json')
        with patch.object(cli, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            after = self.run_cli('insights', '--json')
        self.assertEqual(before[0], 0, before[2])
        self.assertEqual(after, before)

    def test_partial_refresh_keeps_newly_retained_usage_in_evidence(self):
        from unittest.mock import patch
        from tokenatlas import why
        why.CLAUDE_PROJECTS.mkdir(parents=True)
        with patch('tokenatlas.history.utcnow', return_value='2026-09-02T12:00:00Z'):
            self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        path = self.claude()
        with path.open('a') as stream:stream.write('{malformed\n')
        with patch('tokenatlas.history.utcnow', return_value='2026-09-04T12:00:00Z'):
            self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 2)
        code, out, err = self.run_cli('insights', '--json')
        self.assertEqual(code, 0, err)
        evidence = json.loads(out)['token_efficiency']
        self.assertEqual(evidence['coverage']['requests'], 1)
        self.assertEqual(evidence['totals']['known_tokens'], 35)
        self.assertEqual(evidence['window']['snapshot'], '2026-09-04T12:00:00.000Z')

    def test_invalid_settings_fail(self):
        for args in [('--top-turns', '0'), ('--context-threshold', '0'), ('--reduction-percent', '20'),
                     ('--scenario-population', 'subagent_input'),
                     ('--scenario-population', 'subagent_input', '--reduction-percent', 'nan')]:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli('insights', *args)[0], 2)

if __name__ == '__main__':
    unittest.main()
