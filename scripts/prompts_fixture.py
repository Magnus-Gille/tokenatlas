"""Write a synthetic private report with stored prompt previews, for the browser test: python scripts/prompts_fixture.py OUT.html [SHARED.html]
(the optional second file is the same data as a shared report: no prompt text, context or input counts)"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokenatlas.report import build_report, render_report, write_report  # noqa: E402


def obs(i, minute, turn, kind='main', agent='main', model='claude-sonnet-4-5', fresh=1000000, session='s1',
        harness='claude', provider='anthropic', write=0, split=None, out=0, day='2026-09-03', complete=True, synthetic=False):
    return dict(id=f'o{i}', harness=harness, session=session, agent=agent, thread_kind=kind, parent_session=session if kind == 'subagent' else None,
                turn_id=turn, turn_confidence='derived', ts=f'{day}T10:{minute:02d}:00+00:00', model=model, provider=provider,
                machine='m', project_id='/w/codex' if harness == 'codex' else '/w/app', project_label='codex' if harness == 'codex' else 'app', effort=None, origin='cli', raw_usage={'cache_creation': split} if split else {}, tariff=None,
                tokens=dict(fresh_input=fresh, cache_write=write, cache_read=0, output=out, reasoning=0), complete=complete,
                id_synthetic=synthetic, warnings=[], sources=[])


CODEX_SESSION = '019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b'  # a fictional Codex thread id (UUID-shaped, so the report builds a link)
CODEX_CWD = "/w/my app/it's"  # spaces and a quote: the command must be quoted
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)  # fixed so that the 30-day cost-fact window is the same on every run
CONTEXT = {
    ('claude', 's1', 't1'): {'title': 'Fix the <i>build</i> pipeline', 'title_source': 'custom-title', 'cwd': '/w/app', 'branch': 'feat/x',
                             'repository': 'https://example.test/o/app.git',
                             'inputs': {'count': 14, 'first': 'Fix the <b>failing</b> build', 'followups': ['also <u>lint</u>', 'and tests']},
                             'final': 'Done: <b>all green</b> ' + 'after merging the release branch, rerunning the full browser suite and checking every report card twice ' * 3, 'activity': {'shell': 1842, 'edits': 1, 'web': 21, 'subagents': 0},
                             'outcomes': {'prs': ['#16'], 'commits': ['Fix <b>build</b> order', 'Add lint']}},
    ('claude', 's1', 't2'): {'title': None, 'title_source': None, 'cwd': None, 'branch': None, 'repository': None,
                             'inputs': {'count': 3, 'first': None, 'followups': []}, 'final': None,
                             'activity': {'shell': None, 'edits': None, 'web': None, 'subagents': None}, 'outcomes': {'prs': [], 'commits': []}},
    ('codex', CODEX_SESSION, 't4'): {'title': 'Port the importer', 'title_source': 'thread_name', 'cwd': CODEX_CWD, 'branch': None, 'repository': None,
                                     'inputs': {'count': None, 'first': None, 'followups': []}, 'final': None,
                                     'activity': {'shell': None, 'edits': None, 'web': None, 'subagents': None}, 'outcomes': {'prs': [], 'commits': []}},
}


def main(out, shared=None):
    records = [obs(1, 0, 't1'), obs(2, 5, None, 'subagent', 'a1', model='mystery-model'), obs(3, 10, 't2', write=1300000, split=dict(ephemeral_5m_input_tokens=800000, ephemeral_1h_input_tokens=500000)),
               obs(4, 20, 't3', model='mystery-model'), obs(5, 30, 't4', fresh=0, out=500000, session=CODEX_SESSION, harness='codex', provider='openai', model='gpt-5.6-luna'),
               obs(6, 0, None, fresh=2000000, day='2026-06-01', complete=False), obs(7, 1, None, 'subagent', 'a2', session='s9', fresh=1000000, day='2026-06-01'),
               obs(8, 2, None, session='s8', fresh=300000, harness='codex', provider='openai', model='gpt-5.6-luna', day='2026-06-01'),
               obs(9, 3, None, session='s7', fresh=9000000, day='2026-06-01', synthetic=True)]  # ambiguous identity: in no fact  # older than 30 days and without a turn: only the all-history cost facts see it
    # t2: Claude with a 5m/1h cache-write split ($9.00); t4: a non-Claude harness priced from output tokens ($0.60)
    texts = {('claude', 's1', 't1'): 'Fix the <b>failing</b> build', ('claude', 's1', 't2'): 'Refactor the importer'}
    counts = {k: c['inputs']['count'] for k, c in CONTEXT.items()}
    write_report(out, render_report(build_report(records, {}, redact=False, prompt_texts=texts, prompt_context=CONTEXT, prompt_inputs=counts, now=NOW)))
    if shared:write_report(shared, render_report(build_report(records, {}, redact=True, now=NOW)))


if __name__ == '__main__':
    main(*sys.argv[1:3])
