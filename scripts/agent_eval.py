#!/usr/bin/env python3
"""Prepare a synthetic CLI usability eval; optionally run its deterministic smoke baseline.

Usage: python3 scripts/agent_eval.py OUTDIR [--baseline]
No model is invoked. See docs/agent-eval.md for the independent model protocol.
All generated HOME/state, task data and receipts stay under OUTDIR.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import demo


TASKS = [
    ('costliest-yesterday', 'What was the costliest turn yesterday? Return harness, session, turn_id and cost.'),
    ('project-week', 'For last week (2026-09-21 through 2026-09-27 UTC), sum attributed turn list-price cost by project_id. Include the unattributed observation count; do not call this a bill.'),
    ('doctor-codex', 'Diagnose whether Codex is available: where does TokenAtlas read its logs and what does doctor report about that source?'),
    ('rate-thread', 'List rateable threads of demo-orchestrated, then rate the first listed thread as pass for unit agent-eval with note synthetic eval. Return the recorded rating.'),
    ('report-path', 'Where is the latest report? Return its existing path without rebuilding it or opening a browser.'),
    ('compare-days', 'Compare the USD priced cost on 2026-09-23 versus 2026-09-24 UTC. Return both values and the second minus the first; label these list-price estimates.'),
    ('session-total', 'Return the total token count and cost for session demo-orchestrated, including its linked children.'),
    ('snapshot-import', 'Write a private snapshot to SNAPSHOT_PATH and import it with label eval-copy. Return both receipts.'),
    ('overhead', 'Refresh overhead measurements and return which harnesses have overhead data.'),
    ('range-count', 'How many observations are saved for 2026-09-24 UTC? Return the count and coverage_complete; do not assume complete coverage.'),
]


def prepare(outdir, seed=7):
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    home, state = outdir / 'home', outdir / 'state'
    if home.exists() or state.exists():
        raise ValueError('Use a fresh output directory; existing HOME/state is never overwritten.')
    home.mkdir()
    (state / 'tokenatlas').mkdir(parents=True)
    ids = demo.build_home(home, seed)
    db = state / 'tokenatlas/history.sqlite3'
    demo.outcomes(db.with_name('outcomes.jsonl'), ids)
    demo.add_claude_quota(home / '.claude/projects', db.with_name('claude-quota.jsonl'))
    db.with_name('claude-quota.jsonl').chmod(0o600)
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
           'XDG_STATE_HOME': str(state), 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1',
           'TOKENATLAS_NO_BROWSER': '1', 'PYTHONPATH': str(ROOT),
           **{k: os.environ[k] for k in ('SYSTEMROOT',) if k in os.environ}}
    context = {'now': '2026-09-30T12:00:00Z', 'timezone': 'UTC',
               'SNAPSHOT_PATH': str(outdir / 'eval-snapshot.sqlite3')}
    tasks = [{'id': key, 'task': task, 'context': context} for key, task in TASKS]
    (outdir / 'tasks.json').write_text(json.dumps(tasks, indent=2) + '\n')
    (outdir / 'environment.json').write_text(json.dumps(env, indent=2) + '\n')
    for harness in ('claude', 'codex', 'pi', 'opencode'):
        demo.cli(env, db, 'refresh', '--harness', harness, '--json')
    demo.cli(env, db, 'report', '--html', str(db.with_name('report.html')), '--private', '--json')
    return env, db


def baseline(outdir, env, db):
    """Known-command smoke test, NOT a measure of agent discoverability."""
    outdir = Path(outdir).resolve()
    transcript, answers, failures = [], {}, []

    def run(*args):
        proc = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(db), *args, '--json'],
                              cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
        transcript.append(dict(argv=list(args) + ['--json'], exit_code=proc.returncode,
                               stdout=proc.stdout, stderr=proc.stderr))
        if proc.returncode:
            raise AssertionError(f'{args}: exit {proc.returncode}: {proc.stderr}')
        data = json.loads(proc.stdout)
        assert data['schema'] == f'tokenatlas.{args[0]}/1', data
        return data

    def window(start, end):
        return ('--start', start + 'T00:00Z', '--end', end + 'T00:00Z')

    for key, _ in TASKS:
        try:
            if key == 'costliest-yesterday':
                data = run('top', '-n', '1', *window('2026-09-29', '2026-09-30'))['prompts'][0]
                answer = {k: data[k] for k in ('harness', 'session', 'turn_id', 'cost')}
            elif key == 'project-week':
                data = run('top', '-n', '10000', *window('2026-09-21', '2026-09-28'))
                assert len(data['prompts']) == data['total_prompts'], 'Ranking was truncated'
                costs = {}
                for turn in data['prompts']:
                    if turn['cost'] is not None:
                        project = turn['project_id'] or 'unknown'
                        costs[project] = costs.get(project, 0) + turn['cost']
                answer = dict(attributed_list_price_usd=costs, unattributed_observations=data['unattributed_observations'])
            elif key == 'doctor-codex':
                data = run('doctor')
                answer = dict(root=data['roots']['codex'], source_status=data)
            elif key == 'rate-thread':
                listing = run('rate', demo.ORCH)
                thread = listing['threads'][0]['key']
                answer = run('rate', demo.ORCH, '--unit', 'agent-eval', '--thread', thread,
                             '--outcome', 'pass', '--note', 'synthetic eval')
                assert answer['outcome'] == 'pass'
            elif key == 'report-path':
                answer = run('show')
                assert Path(answer['html']).is_file()
                assert answer['shown'] is False
            elif key == 'compare-days':
                values = []
                for day, end in [('2026-09-23', '2026-09-24'), ('2026-09-24', '2026-09-25')]:
                    data = run('insights', *window(day, end))
                    cost = next(f['values']['priced_cost'] for f in data['facts'] if f['id'] == 'cost_parts')
                    values.append(cost)
                answer = dict(list_price_usd=values, difference=values[1] - values[0])
            elif key == 'session-total':
                answer = run('session', demo.ORCH)['total']
            elif key == 'snapshot-import':
                path = str(outdir / 'eval-snapshot.sqlite3')
                answer = dict(snapshot=run('snapshot', path), imported=run('import', path, '--label', 'eval-copy'))
            elif key == 'overhead':
                answer = sorted(run('overhead', '--refresh')['harnesses'])
                assert answer
            elif key == 'range-count':
                data = run('report', *window('2026-09-24', '2026-09-25'), '--timezone', 'UTC')
                answer = dict(observations=data['totals']['observations'], coverage_complete=data['coverage_complete'])
                assert answer['observations'] > 0 and not answer['coverage_complete']
            answers[key] = answer
        except (AssertionError, KeyError, IndexError, StopIteration, ValueError, subprocess.TimeoutExpired) as exc:
            failures.append(dict(task=key, title=f'Agent CLI eval: {key}', evidence=str(exc)))
    summary = dict(kind='deterministic-smoke-not-model-eval', passed=len(answers), total=len(TASKS),
                   model_eval='not run', failures=failures)
    for name, data in [('baseline', summary), ('answers', answers), ('transcript', transcript), ('tickets', failures)]:
        (outdir / f'{name}.json').write_text(json.dumps(data, indent=2) + '\n')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outdir', type=Path, help='Fresh disposable output directory.')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--baseline', action='store_true', help='Also run known-command smoke checks (not a model eval).')
    args = parser.parse_args(argv)
    env, db = prepare(args.outdir, args.seed)
    if args.baseline:
        summary = baseline(args.outdir, env, db)
        print(json.dumps(summary, indent=2))
        return bool(summary['failures'])
    print(f'Prepared tasks and isolated environment under {args.outdir.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
