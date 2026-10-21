import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_why_harnesses import create_opencode_db, pi_message, write_pi_session
from tokenatlas import overhead
from tokenatlas.__main__ import main

SECRET = 'SECRET-MARKER-9f3a'
PRIVATE = 'PRIVATE-PATH-zz'


def pad(n):
    """Content of exactly n characters that carries the secret marker."""
    return (SECRET + 'x' * n)[:n]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


def att(kind, ts='2026-09-10T10:00:00.000Z', **fields):
    return {'type': 'attachment', 'timestamp': ts, 'attachment': {'type': kind, **fields}}


def asst(req, msg, ts, usage, content=()):
    return {'type': 'assistant', 'timestamp': ts, 'requestId': req,
            'message': {'id': msg, 'role': 'assistant', 'usage': usage, 'content': list(content)}}


def claude_root(tmp):
    root = Path(tmp) / 'projects'
    u1 = {'input_tokens': 10, 'cache_creation_input_tokens': 5000, 'cache_read_input_tokens': 0}
    u2 = {'input_tokens': 4, 'cache_creation_input_tokens': 100, 'cache_read_input_tokens': 5010}
    tool = {'type': 'tool_use', 'id': 'tu1', 'name': 'Skill', 'input': {'skill': 'pdf', 'args': pad(7)}}
    write_jsonl(root / PRIVATE / 's1.jsonl', [
        att('skill_listing', content=pad(400), skillCount=3, names=['a', 'b', 'c'], isInitial=True),
        att('instructions', files=[{'path': f'/home/u/{PRIVATE}/AGENTS.md', 'type': 'User', 'content': pad(100)},
                                   {'path': f'/home/u/{PRIVATE}/proj/CLAUDE.md', 'type': 'Project', 'content': pad(60)}]),
        att('nested_memory', path=f'/home/u/{PRIVATE}/sub/CLAUDE.md', content=pad(20), displayPath='sub/CLAUDE.md'),
        att('mcp_instructions_delta', addedNames=['srv'], addedBlocks=[pad(50)]),
        att('deferred_tools_delta', addedNames=['T1', 'T2'], addedLines=[pad(30), pad(10)]),
        att('agent_listing_delta', addedTypes=['x'], addedLines=[pad(15)]),
        att('prompt_snapshot', systemPrompt=[pad(1000), pad(500)]),
        att('session_context', context=pad(25)),
        asst('r1', 'm1', '2026-09-10T10:00:01.000Z', u1, [tool]),
        {'type': 'user', 'isMeta': True, 'sourceToolUseID': 'tu1', 'timestamp': '2026-09-10T10:00:02.000Z',
         'message': {'role': 'user', 'content': [{'type': 'text', 'text': pad(300)}]}},
        asst('r2', 'm2', '2026-09-10T10:00:03.000Z', u2),
        asst('r2', 'm2', '2026-09-10T10:00:04.000Z', u2),  # streamed duplicate
    ])
    write_jsonl(root / PRIVATE / 's1' / 'subagents' / 'agent-a1.jsonl', [
        att('prompt_snapshot', ts='2026-09-10T10:05:00.000Z', systemPrompt=[pad(200)]),
        asst('q1', 'n1', '2026-09-10T10:05:01.000Z',
             {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 900}),
    ])
    return root


def skill_body(name, size):
    head = f'---\nname: {name}\ndescription: d\n---\n'
    return head + pad(size - len(head))


def codex_exec_root(tmp, calls):
    """Codex rollout whose exec calls are (command, output) pairs."""
    rows = [{'type': 'session_meta', 'timestamp': '2026-09-10T11:00:00Z', 'payload': {'id': 'cx', 'timestamp': '2026-09-10T11:00:00Z'}}]
    for i, (command, output) in enumerate(calls):
        rows.append({'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'custom_tool_call', 'name': 'exec', 'call_id': f'k{i}', 'input': command}})
        rows.append({'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': f'k{i}', 'output': output}})
    write_jsonl(Path(tmp) / 'codex' / 'rollout-cx.jsonl', rows)
    return Path(tmp) / 'codex'


def codex_row(calls):
    with tempfile.TemporaryDirectory() as tmp:
        [row] = overhead.scan('codex', codex_exec_root(tmp, calls))
    return row


def codex_root(tmp):
    root = Path(tmp) / 'codex'
    skills = '<skills_instructions>' + pad(80) + '</skills_instructions>'
    agents = '<INSTRUCTIONS>' + pad(70) + '</INSTRUCTIONS>'
    env = '<environment_context>' + pad(33) + '</environment_context>'

    def msg(role, text):
        return {'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}]}}

    def count(last, total):
        info = None if last is None else {'last_token_usage': last, 'total_token_usage': {'total_tokens': total}}
        return {'type': 'event_msg', 'timestamp': 't', 'payload': {'type': 'token_count', 'info': info}}
    first = {'input_tokens': 3000, 'cached_input_tokens': 2000, 'output_tokens': 5}
    write_jsonl(root / '2026' / 'rollout-c1.jsonl', [
        {'type': 'session_meta', 'timestamp': '2026-09-10T11:00:00Z', 'payload': {
            'id': 'c1', 'timestamp': '2026-09-10T11:00:00Z',
            'base_instructions': {'text': pad(800), 'provenance': 'x'}}},
        msg('developer', 'permissions ' + pad(40) + ' ' + skills),
        msg('user', agents + '\n' + env),
        count(None, 0), count(first, 3005), count(first, 3005),
        count({'input_tokens': 3100, 'cached_input_tokens': 3000, 'output_tokens': 5}, 6110),
        {'type': 'response_item', 'timestamp': '2026-09-10T11:00:05Z', 'payload': {
            'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'k1',
            'input': 'cat /home/u/.codex/skills/pdf/SKILL.md'}},
        {'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'k1', 'output': skill_body('pdf', 300)}},
        {'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'k2', 'input': 'ls'}},
        {'type': 'response_item', 'timestamp': 't', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'k2', 'output': pad(9)}},
    ])
    return root, len(skills), len(agents), len(env)


def opencode_db(tmp):
    path = Path(tmp) / 'opencode.db'
    con = create_opencode_db(path)
    con.execute('CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,'
                ' time_created INTEGER, time_updated INTEGER, data TEXT)')
    con.execute("INSERT INTO session VALUES ('o1','p',NULL,'/w','1',1,2)")
    for i, (created, tokens) in enumerate(((1000, {'input': 4, 'output': 1, 'cache': {'read': 50, 'write': 10}}),
                                            (2000, {'input': 2, 'output': 1, 'cache': {'read': 64, 'write': 0}}))):
        con.execute('INSERT INTO message VALUES (?,?,?,?,?)', (f'm{i}', 'o1', created, created,
                    json.dumps({'role': 'assistant', 'tokens': tokens, 'text': SECRET})))
    skill = {'type': 'tool', 'tool': 'skill', 'state': {'input': {'name': 'pdf'}, 'output': pad(210)}}
    other = {'type': 'tool', 'tool': 'bash', 'state': {'input': {}, 'output': pad(5)}}
    for i, data in enumerate((skill, other)):
        con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', (f'p{i}', 'm0', 'o1', 1500, 1500, json.dumps(data)))
    con.commit()
    con.close()
    return path


def by_session(rows):
    return {r['session']: r for r in rows}


class ScanTests(unittest.TestCase):
    def test_claude_exact_sizes_floor_and_skill_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = by_session(overhead.scan('claude', claude_root(tmp)))
        self.assertEqual(set(rows), {'s1', 's1/agent-a1'})
        main_s, sub = rows['s1'], rows['s1/agent-a1']
        self.assertFalse(main_s['is_subagent'])
        self.assertTrue(sub['is_subagent'])
        digest = lambda p: hashlib.sha256(p.encode()).hexdigest()[:10]
        want = {('skill_listing', '3 skills'): 400,
                ('instructions', f'AGENTS.md#{digest(f"/home/u/{PRIVATE}/AGENTS.md")}'): 100,
                ('instructions', f'CLAUDE.md#{digest(f"/home/u/{PRIVATE}/proj/CLAUDE.md")}'): 60,
                ('nested_memory', f'CLAUDE.md#{digest(f"/home/u/{PRIVATE}/sub/CLAUDE.md")}'): 20,
                ('mcp_instructions', 'srv'): 50, ('deferred_tools', 'T1,T2'): 40,
                ('agent_listing', 'x'): 15, ('system_prompt', 'system_prompt'): 1500,
                ('session_context', 'session_context'): 25}
        got = {(c['kind'], c['name']): c['chars'] for c in main_s['components']}
        self.assertEqual(got, want)
        self.assertEqual({c['source'] for c in main_s['components']}, {'exact'})
        self.assertEqual(main_s['floor_tokens'], 5010)
        self.assertEqual(main_s['floor_breakdown'], {'input': 10, 'cache_write': 5000, 'cache_read': 0})
        self.assertEqual(main_s['calls'], 2)
        self.assertEqual(main_s['first_ts'], '2026-09-10T10:00:00.000Z')
        self.assertEqual(main_s['skill_uses'],
                         [{'name': 'pdf', 'chars': 300, 'ts': '2026-09-10T10:00:02.000Z', 'source': 'exact'}])
        self.assertEqual(sub['floor_tokens'], 901)
        self.assertEqual((sub['calls'], sub['components'][0]['chars']), (1, 200))

    def test_workflow_subagent_keyed_to_session_above_subagents_and_joins(self):
        usage = {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 900}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'projects'
            write_jsonl(root / PRIVATE / 's1' / 'subagents' / 'workflows' / 'wf_x' / 'agent-w1.jsonl', [
                asst('q1', 'n1', '2026-09-10T10:05:01.000Z', usage)])
            rows = overhead.scan('claude', root)
        self.assertEqual([r['session'] for r in rows], ['s1/agent-w1'])
        self.assertTrue(rows[0]['is_subagent'])
        joined = {('claude', ('s1', 'file', 'w1')): {'input': 1}}
        self.assertEqual(overhead._lookup(joined, 'claude', rows[0]['session'], True), ({'input': 1}, None))

    def test_codex_blocks_floor_and_heuristic_skill_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, skills, agents, env = codex_root(tmp)
            [row] = overhead.scan('codex', root)
        got = {c['kind']: (c['chars'], c['source']) for c in row['components']}
        self.assertEqual(got, {'system_prompt': (800, 'exact'), 'skills_listing': (skills, 'exact'),
                               'agents_md': (agents, 'exact'), 'environment_context': (env, 'exact')})
        self.assertEqual(row['session'], 'c1')
        self.assertEqual(row['floor_tokens'], 3000)
        self.assertEqual(row['floor_breakdown'], {'input': 1000, 'cache_write': 0, 'cache_read': 2000})
        self.assertEqual(row['calls'], 2)
        self.assertEqual(row['skill_uses'],
                         [{'name': 'pdf', 'chars': 300, 'ts': '2026-09-10T11:00:05Z', 'source': 'heuristic'}])

    def test_pi_reports_floor_only(self):
        zero = {'input': 0, 'cacheRead': 0, 'cacheWrite': 0, 'output': 0}
        usage = {'input': 5, 'cacheRead': 100, 'cacheWrite': 20, 'output': 3}
        with tempfile.TemporaryDirectory() as tmp:
            write_pi_session(Path(tmp) / 'a.jsonl', 'pi1', '2026-09-10T09:00:00Z', '/w', [
                pi_message('e1', 'r0', '2026-09-10T09:00:01Z', zero),
                pi_message('e2', 'r1', '2026-09-10T09:00:02Z', usage),
                pi_message('e3', 'r2', '2026-09-10T09:00:03Z', usage)])
            [row] = overhead.scan('pi', Path(tmp))
        self.assertEqual((row['session'], row['floor_tokens'], row['calls']), ('pi1', 125, 2))
        self.assertEqual(row['floor_breakdown'], {'input': 5, 'cache_write': 20, 'cache_read': 100})
        self.assertEqual((row['components'], row['skill_uses']), ([], []))

    def test_opencode_skill_output_and_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            [row] = overhead.scan('opencode', opencode_db(tmp))
        self.assertEqual((row['session'], row['floor_tokens'], row['calls']), ('o1', 64, 2))
        self.assertEqual(row['floor_breakdown'], {'input': 4, 'cache_write': 10, 'cache_read': 50})
        self.assertEqual(row['skill_uses'], [{'name': 'pdf', 'chars': 210, 'ts': '1970-01-01T00:00:01.500Z',
                                              'source': 'exact'}])
        self.assertEqual(row['components'], [])


class CodexSkillReadTests(unittest.TestCase):
    BODY = skill_body('x', 300)

    def assertUnidentified(self, command, output, count=1):
        row = codex_row([(command, output)])
        self.assertEqual(row['skill_uses'], [])
        self.assertEqual(row['unidentified_skill_reads'], count)

    def test_junk_patterns_yield_only_unidentified(self):
        listing = 'index\nskills\n' + 'a-skill-name\n' * 30
        for command, output in (
                ('for p in ~/.codex/skills/*; do cat "$p/SKILL.md"; done', self.BODY),
                ("printf '%s' ~/.codex/skills/%s/SKILL.md", self.BODY),
                ('cat /Users/x/.codex/skills/$p/SKILL.md', self.BODY),
                ('cat "/Users/x/.codex/skills/steg-${PROCESS_FLOWS.indexOf(flow)+1}/SKILL.md"', self.BODY),
                ('cat ~/.codex/skills/{issues,eli5,review-pr-codex}/SKILL.md', self.BODY),
                ("grep -E 'inspect_pr_checks.py$|gh-fix-ci' ~/.codex/skills/SKILL.md", self.BODY),
                ('cat ~/.codex/skills/magnus-security-review}/SKILL.md', self.BODY),
                ('find ~/.codex/skills -name SKILL.md', listing),
                ('ls /Users/x/.codex/skills/eli5/SKILL.md ~/.codex/skills', listing),
                ('cat /Users/x/.codex/skills/eli5/SKILL.md', pad(49)),
                ('cat /Users/x/.codex/skills/eli5/SKILL.md', '---\nname: eli5\n---\n' + pad(10)),
                ('cat /Users/x/.codex/skills/eli5/SKILL.md', 'not a body ' + pad(300)),
                ('cat /Users/x/.codex/skills/eli5/SKILL.md', '---\ndescription: d\n---\n' + pad(300)),
                ('cat /Users/x/.codex/notskills/eli5/SKILL.md', self.BODY)):
            with self.subTest(command=command):
                self.assertUnidentified(command, output)

    def test_valid_forms_are_named(self):
        gutter = ''.join(f'{i:6d}\u2192{line}\n' for i, line in enumerate(self.BODY.split('\n'), 1))
        tabbed = ''.join(f'{i}\t{line}\n' for i, line in enumerate(skill_body('review-pr-codex', 260).split('\n'), 1))
        row = codex_row([
            ("sed -n '1,200p' /Users/x/.codex/skills/eli5/SKILL.md", '# eli5\n' + pad(300)),
            ('cat ~/.agents/skills/review-pr-codex/SKILL.md', skill_body('review-pr-codex', 260)),
            ('cat /Users/x/.codex/skills/gutter/SKILL.md', gutter),
            ('cat /Users/x/.codex/skills/tabbed/SKILL.md', tabbed),
            ('cat /Users/x/.codex/skills/dir-name/SKILL.md', skill_body('real-name', 250))])
        self.assertEqual([(u['name'], u['source']) for u in row['skill_uses']],
                         [('eli5', 'heuristic'), ('review-pr-codex', 'heuristic'), ('x', 'heuristic'),
                          ('review-pr-codex', 'heuristic'), ('real-name', 'heuristic')])
        self.assertEqual(row['unidentified_skill_reads'], 0)
        self.assertEqual(row['skill_uses'][1]['chars'], 260)

    def test_list_shaped_outputs(self):
        def items(*texts):
            return [{'type': 'input_text', 'text': t} for t in texts]
        header = 'Script completed\nWall time 0.1 seconds\nOutput:'
        listing = 'a\nb\nc\n'
        cases = [
            ('cat /u/skills/x/SKILL.md', items(header, self.BODY), [('x', 300)]),
            ('ls /u/skills/x; cat /u/skills/y/SKILL.md', items(header, listing, skill_body('y', 250)), [('y', 250)]),
            ('cat /u/skills/x/SKILL.md', items(header, 'short'), []),
            ('cat /u/skills/x/SKILL.md', header + '\n' + self.BODY, [('x', 300)]),
            ('cat /u/skills/a/SKILL.md; cat /u/skills/b/SKILL.md',
             items(header, skill_body('b', 250), skill_body('a', 220)), [('a', 220), ('b', 250)])]
        for command, output, expected in cases:
            with self.subTest(command=command, output=str(output)[:40]):
                row = codex_row([(command, output)])
                self.assertEqual([(u['name'], u['chars']) for u in row['skill_uses']], expected)
                self.assertEqual(row['unidentified_skill_reads'], 0 if expected else 1)

    def test_multiple_concrete_paths_split_output_equally(self):
        row = codex_row([('cat /a/skills/x/SKILL.md ~/b/skills/y/SKILL.md', self.BODY)])
        self.assertEqual([(u['name'], u['chars'], u['source']) for u in row['skill_uses']],
                         [('x', 150, 'heuristic'), ('y', 150, 'heuristic')])

    def test_unidentified_counter_persists_and_is_reported(self):
        junk = ('cat ~/.codex/skills/{a,b}/SKILL.md', self.BODY)
        with tempfile.TemporaryDirectory() as tmp:
            root = codex_exec_root(tmp, [junk, junk, ('cat /u/skills/ok/SKILL.md', skill_body('ok', 300))])
            con = sqlite3.connect(':memory:')
            overhead.save(con, 'codex', overhead.scan('codex', root))
            result = overhead.summarize(con)
        codex = result['harnesses']['codex']
        self.assertEqual(codex['unidentified_skill_reads'], 2)
        self.assertEqual([u['name'] for u in codex['skill_uses']], ['ok'])
        self.assertIn('unidentified skill reads: 2', overhead.render(result))


class StoreTests(unittest.TestCase):
    def test_no_content_or_paths_stored_and_rescan_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude, (codex, *_) = claude_root(tmp), codex_root(tmp)
            oc = opencode_db(tmp)
            db = Path(tmp) / 'h.sqlite3'
            con = sqlite3.connect(db)
            for _ in range(2):
                for h, root in (('claude', claude), ('codex', codex), ('opencode', oc)):
                    overhead.save(con, h, overhead.scan(h, root))
            counts = lambda: [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                              for t in ('overhead_sessions', 'overhead_items', 'overhead_skill_uses')]
            first = counts()
            overhead.save(con, 'claude', overhead.scan('claude', claude))
            self.assertEqual(counts(), first)
            self.assertEqual(first[0], 4)
            con.commit()
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            con.close()
            blob = db.read_bytes()
        for needle in (SECRET, PRIVATE, '/home/u', 'SKILL.md'):
            self.assertNotIn(needle.encode(), blob)


PRICES = {'schema': 1, 'retrieved_on': '2026-09-29', 'unit': 'per_million_tokens', 'provider_aliases': {},
          'local_providers': [], 'models': [{
              'provider': 'p', 'model': 'm-x', 'currency': 'USD', 'input': 1.0, 'cache_read': 2.0, 'output': 3.0,
              'free': False, 'source_url': 'x', 'retrieved_on': '2026-09-29'}]}


def sess(name, floor, calls, comps=(), harness='claude', ts='2026-09-10T10:00:00Z', skills=()):
    return {'harness': harness, 'session': name, 'is_subagent': False, 'first_ts': ts, 'floor_tokens': floor,
            'floor_breakdown': {'input': floor, 'cache_write': 0, 'cache_read': 0}, 'calls': calls,
            'components': [{'kind': k, 'name': k, 'chars': c, 'source': 'exact'} for k, c in comps],
            'skill_uses': [{'name': n, 'chars': c, 'ts': ts, 'source': 'exact'} for n, c in skills]}


class SummaryTests(unittest.TestCase):
    def db(self, rows, harness='claude'):
        con = sqlite3.connect(':memory:')
        overhead.save(con, harness, rows)
        return con

    def test_floor_distribution_components_residual_and_skills(self):
        rows = [sess(f's{i}', 100 * i, 1, [('system_prompt', 40)], skills=[('pdf', 100 * i)] if i < 4 else [])
                for i in range(1, 6)]
        rep = overhead.summarize(self.db(rows))['harnesses']['claude']
        self.assertEqual(rep['floor'], {'n': 5, 'median': 300, 'p10': 140, 'p90': 460})
        comp = rep['components']['system_prompt']
        self.assertEqual((comp['n'], comp['median_chars'], comp['estimated_tokens'], comp['label']),
                         (5, 40, 10, 'estimate'))
        self.assertEqual(rep['residual']['median_tokens'], 290)
        self.assertEqual(rep['residual']['label'], 'other: tools, first prompt, unlogged')
        [skill] = rep['skill_uses']
        self.assertEqual((skill['name'], skill['count'], skill['median_chars'], skill['estimated_tokens']),
                         ('pdf', 3, 200, 50.0))

    def test_recurring_cost_uses_cache_read_price_and_dominant_model(self):
        rows = [sess('a', 1_000_000, 4), sess('b', 1_000_000, 1), sess('c', 500, 3)]
        records = [{'harness': 'claude', 'session': 'a', 'provider': 'p', 'model': 'm-x'}] * 3 + [
            {'harness': 'claude', 'session': 'a', 'provider': 'p', 'model': 'other'},
            {'harness': 'claude', 'session': 'b', 'provider': 'p', 'model': 'm-x'}]
        rep = overhead.summarize(self.db(rows), records=records, prices=PRICES)['harnesses']['claude']
        cost = rep['recurring_cost']
        self.assertAlmostEqual(cost['cost']['USD'], 6.0)  # 1M floor x 3 re-reads x 2.0 per MTok; b has 0 re-reads
        self.assertEqual((cost['sessions_priced'], cost['sessions_unpriced']), (2, 1))
        self.assertEqual(cost['label'], 'API-equivalent at list price, estimate')
        self.assertIsNone(overhead.summarize(self.db(rows))['harnesses']['claude']['recurring_cost']['cost'])

    def claude_prices(self):
        model = {'provider': 'anthropic', 'model': 'claude-opus-5', 'currency': 'USD', 'input': 5.0, 'cache_read': 0.5,
                 'output': 25.0, 'cache_write_5m': 6.0, 'cache_write_1h': 10.0, 'free': False, 'source_url': 'x',
                 'retrieved_on': '2026-09-29', 'modifiers': {'speed=fast': {'cache_read': 5.0},
                                                            'inference_geo=us': {'multiplier': 1.1}}}
        return dict(PRICES, models=[model])

    def recurring(self, tariffs):
        rows = [sess('a', 1_000_000, 2)]
        records = [{'harness': 'claude', 'session': 'a', 'provider': 'anthropic', 'model': 'claude-opus-5', 'tariff': t}
                   for t in tariffs]
        return overhead.summarize(self.db(rows), records=records, prices=self.claude_prices())['harnesses']['claude']['recurring_cost']

    def test_recurring_cost_uses_captured_tariff(self):
        self.assertAlmostEqual(self.recurring([{'speed': 'fast'}])['cost']['USD'], 5.0)
        self.assertAlmostEqual(self.recurring([{'speed': 'standard', 'inference_geo': 'us'}])['cost']['USD'], 0.55)
        self.assertAlmostEqual(self.recurring([{'speed': 'standard'}])['cost']['USD'], 0.5)

    def test_mixed_tariffs_use_dominant_and_are_flagged(self):
        got = self.recurring([{'speed': 'fast'}, {'speed': 'fast'}, {'speed': 'standard'}])
        self.assertAlmostEqual(got['cost']['USD'], 5.0)
        self.assertIn('mixed tariffs in session', got['assumptions'])
        self.assertNotIn('mixed tariffs in session', self.recurring([{'speed': 'fast'}] * 2)['assumptions'])

    def test_currencies_stay_separate_in_cost_and_share(self):
        eur = dict(PRICES['models'][0], model='m-e', currency='EUR', cache_read=1.0)
        prices = dict(PRICES, models=PRICES['models'] + [eur])
        rows = [sess('a', 1_000_000, 2, harness='pi'), sess('b', 1_000_000, 2, harness='pi')]
        records = [rec('pi', 'a', 1_000_000, 0, 0, 10), rec('pi', 'a', 10, 0, 1_000_000, 10),
                   rec('pi', 'b', 1_000_000, 0, 0, 10, model='m-e'), rec('pi', 'b', 10, 0, 1_000_000, 10, model='m-e')]
        con = sqlite3.connect(':memory:')
        overhead.save(con, 'pi', rows)
        res = overhead.summarize(con, records=records, prices=prices)
        rc = res['harnesses']['pi']['recurring_cost']
        self.assertEqual(set(rc['cost']), {'USD', 'EUR'})
        self.assertAlmostEqual(rc['cost']['USD'], 2.0); self.assertAlmostEqual(rc['cost']['EUR'], 1.0)
        share = res['harnesses']['pi']['relative']['fixed_cost_share']['pct']
        self.assertEqual(set(share), {'USD', 'EUR'})
        json.dumps(res)
        text = overhead.render(res)
        self.assertIn('EUR 1.00, USD 2.00', text)
        self.assertRegex(text, r'EUR \d+\.\d%, USD \d+\.\d%')

    def test_filters_and_missing_components(self):
        rows = [sess('old', 10, 1, ts='2026-08-01T00:00:00Z'), sess('new', 20, 1, ts='2026-09-10T00:00:00Z')]
        con = self.db(rows)
        overhead.save(con, 'pi', [sess('p', 30, 1, harness='pi')])
        got = overhead.summarize(con, since='2026-09-01T00:00:00Z', harness='claude')['harnesses']
        self.assertEqual(list(got), ['claude'])
        self.assertEqual(got['claude']['floor']['n'], 1)
        pi = overhead.summarize(con, harness='pi')['harnesses']['pi']
        self.assertFalse(pi['components_available'])
        self.assertIsNone(pi['residual']['median_tokens'])


def rec(harness, session, fresh, write, read, out, model='m-x', **extra):
    return {'harness': harness, 'session': session, 'provider': 'p', 'model': model, 'thread_kind': 'main',
            'tokens': {'fresh_input': fresh, 'cache_write': write, 'cache_read': read, 'output': out},
            'warnings': [], **extra}


def profile(harness, session, k, **extra):
    """Two calls, floor 100k: input 210k (fixed 200k = 95.2%), output 20k, cost 370k micro-dollars (fixed 200k)."""
    return sess(session, 100 * k, 2, harness=harness), [rec(harness, session, 100 * k, 0, 0, 10 * k, **extra),
                                                        rec(harness, session, 10 * k, 0, 100 * k, 10 * k, **extra)]


class RelativeTests(unittest.TestCase):
    def build(self, specs, records):
        con = sqlite3.connect(':memory:')
        for h in {s['harness'] for s in specs}:
            overhead.save(con, h, [s for s in specs if s['harness'] == h])
        return overhead.summarize(con, records=records, prices=PRICES)

    def two(self):
        (a, ra), (b, rb) = profile('pi', 'a', 1), profile('opencode', 'b', 3)
        return self.build([a, b], ra + rb)

    def test_relative_equal_totals_differ(self):
        res = self.two()['harnesses']
        rel = {h: res[h]['relative'] for h in res}
        for key, field in (('fixed_share_of_input', 'pct'), ('fixed_per_1k_output', 'value')):
            self.assertAlmostEqual(rel['pi'][key][field], rel['opencode'][key][field])
        self.assertAlmostEqual(rel['pi']['fixed_share_of_input']['pct'], 200 / 210 * 100)
        self.assertAlmostEqual(rel['pi']['fixed_share_of_input']['median_session_pct'], 200 / 210 * 100)
        self.assertAlmostEqual(rel['pi']['fixed_per_1k_output']['value'], 10000)
        self.assertAlmostEqual(rel['pi']['fixed_cost_share']['pct']['USD'], 200 / 370 * 100)
        self.assertAlmostEqual(rel['opencode']['fixed_cost_share']['pct']['USD'], 200 / 370 * 100)
        self.assertEqual(rel['pi']['calls_per_session'], {'median': 2, 'p90': 2, 'n': 1})
        self.assertEqual(rel['pi']['floor_per_call']['median'], 100)
        self.assertEqual(rel['opencode']['floor_per_call']['median'], 300)
        self.assertAlmostEqual(res['opencode']['recurring_cost']['cost']['USD'], 3 * res['pi']['recurring_cost']['cost']['USD'])
        self.assertEqual(rel['pi']['fixed_share_of_input']['n'], 1)

    def test_fixed_capped_at_input_total(self):
        spec = sess('c', 100, 5, harness='pi')
        res = self.build([spec], [rec('pi', 'c', 100, 0, 0, 5), rec('pi', 'c', 0, 0, 200, 5)])
        rel = res['harnesses']['pi']['relative']
        self.assertAlmostEqual(rel['fixed_share_of_input']['pct'], 100.0)  # 500 capped to 300
        self.assertAlmostEqual(rel['fixed_per_1k_output']['value'], 300 / 10 * 1000)

    def test_exclusions_with_reasons(self):
        good, rg = profile('pi', 'g', 1)
        specs = [good, sess('nohist', 100, 2, harness='pi'), sess('unk', 100, 2, harness='pi'),
                 sess('nocost', 100, 2, harness='pi'), sess('nofloor', None, 0, harness='pi')]
        records = rg + [rec('pi', 'unk', None, 0, 0, 5), rec('pi', 'nocost', 10, 0, 0, 5, model='other')]
        rel = self.build(specs, records)['harnesses']['pi']['relative']
        self.assertEqual(rel['excluded']['input'], {'no_history_records': 1, 'tokens_unknown': 1, 'no_floor': 1})
        self.assertEqual(rel['excluded']['cost'], {'no_history_records': 1, 'tokens_unknown': 1, 'no_floor': 1,
                                                   'cost_unknown': 1})
        self.assertEqual((rel['fixed_share_of_input']['n'], rel['fixed_cost_share']['n']), (2, 1))

    def test_claude_subagents_matched_or_excluded(self):
        main = sess('p1', 100, 2, harness='claude')
        sub_ok, sub_bad = sess('p1/agent-abc', 50, 2, harness='claude'), sess('p1/agent-zzz', 50, 2, harness='claude')
        sub_ok['is_subagent'] = sub_bad['is_subagent'] = True
        records = [rec('claude', 'p1', 100, 0, 0, 5), rec('claude', 'p1', 10, 0, 100, 5),
                   rec('claude', 'p1', 50, 0, 0, 5, thread_kind='subagent', agent='abc'),
                   rec('claude', 'p1', 5, 0, 50, 5, thread_kind='subagent', agent='abc')]
        rel = self.build([main, sub_ok, sub_bad], records)['harnesses']['claude']['relative']
        self.assertEqual(rel['fixed_share_of_input']['n'], 2)  # main sums exclude subagent rows
        self.assertAlmostEqual(rel['fixed_share_of_input']['pct'], (200 + 100) / (210 + 105) * 100)
        self.assertEqual(rel['excluded']['input'], {'subagent_unmatched': 1})

    def test_claude_subagents_match_by_transcript_path_not_type(self):
        main = sess('p1', 100, 2, harness='claude')
        subs = [sess(f'p1/agent-{i}', 50, 2, harness='claude') for i in ('abc123', 'def456')]
        for x in subs:
            x['is_subagent'] = True
        base = '/x/p1/subagents/'
        records = [rec('claude', 'p1', 100, 0, 0, 5), rec('claude', 'p1', 10, 0, 100, 5)]
        for path, k in ((base + 'agent-abc123.jsonl', 1), (base + 'workflows/wf_9/agent-def456.jsonl', 2)):
            records += [rec('claude', 'p1', 50 * k, 0, 0, 5, thread_kind='subagent', agent='implementer', sources=[path]),
                        rec('claude', 'p1', 5 * k, 0, 50 * k, 5, thread_kind='subagent', agent='implementer', sources=[path])]
        rel = self.build([main] + subs, records)['harnesses']['claude']['relative']
        self.assertEqual(rel['excluded']['input'], {})
        self.assertEqual(rel['subagents'], {'sessions': 2, 'matched': 2, 'excluded': 0})
        self.assertEqual(rel['fixed_share_of_input']['n'], 3)
        # abc123 has input 105 (fixed 100), def456 input 210 but fixed capped at floor x calls = 100: not merged by type
        self.assertAlmostEqual(rel['fixed_share_of_input']['pct'], (200 + 100 + 100) / (210 + 105 + 210) * 100)

    def test_windows_import_paths_join_by_agent_id(self):
        main = sess('p1', 100, 2, harness='claude')
        sub = sess('p1/agent-abc', 50, 2, harness='claude'); sub['is_subagent'] = True
        path = 'm-pc:C:\\Users\\x\\.claude\\projects\\p\\p1\\subagents\\workflows\\wf_1\\agent-abc.jsonl'
        records = [rec('claude', 'p1', 100, 0, 0, 5), rec('claude', 'p1', 10, 0, 100, 5),
                   rec('claude', 'p1', 50, 0, 0, 5, thread_kind='subagent', agent='implementer', sources=[path]),
                   rec('claude', 'p1', 5, 0, 50, 5, thread_kind='subagent', agent='implementer', sources=[path])]
        rel = self.build([main, sub], records)['harnesses']['claude']['relative']
        self.assertEqual(rel['subagents'], {'sessions': 1, 'matched': 1, 'excluded': 0})

    def test_output_lower_bound_marked(self):
        a, ra = profile('pi', 'a', 1)
        b, rb = profile('claude', 'b', 1)
        rb[0]['warnings'] = ['output_not_final']
        res = self.build([a, b], ra + rb)['harnesses']
        self.assertFalse(res['pi']['relative']['fixed_per_1k_output']['output_lower_bound'])
        self.assertTrue(res['claude']['relative']['fixed_per_1k_output']['output_lower_bound'])
        self.assertIn('lower bound', overhead.render(self.build([a, b], ra + rb)))

    def test_sorted_table_and_header_note(self):
        lo, rlo = profile('pi', 'a', 1)  # 95.2%
        hi = sess('h', 100, 2, harness='opencode')
        rhi = [rec('opencode', 'h', 100, 0, 0, 1), rec('opencode', 'h', 900, 0, 100, 1)]  # 200 / 1100
        text = overhead.render(self.build([hi, lo], rlo + rhi))
        self.assertTrue(text.startswith(
            'Relative measures are usage-normalized. Token counts come from each provider\'s own tokenizer (Claude 4.7+ '
            'counts about 30% more tokens for the same text), so compare percentages across providers rather than raw tokens.'))
        head = text.split('\n\n')[1]
        self.assertIn('fixed % of input', head)
        self.assertLess(head.index('opencode  '), head.index('pi  '))
        self.assertIn('recurring re-read cost (total, depends on usage):', text)

    def test_json_fields_and_no_records(self):
        data = json.loads(json.dumps(self.two()))
        rel = data['harnesses']['pi']['relative']
        self.assertEqual(set(rel), {'floor_per_call', 'fixed_share_of_input', 'fixed_per_1k_output', 'fixed_cost_share',
                                    'calls_per_session', 'excluded', 'subagents'})
        self.assertEqual(set(rel['floor_per_call']), {'median', 'p10', 'p90', 'n'})
        con = sqlite3.connect(':memory:')
        overhead.save(con, 'pi', [sess('a', 100, 2, harness='pi')])
        none = overhead.summarize(con)['harnesses']['pi']['relative']
        self.assertIsNone(none['fixed_share_of_input']['pct'])
        self.assertEqual(none['excluded']['input'], {'no_history_records': 1})


class CliTests(unittest.TestCase):
    def test_refresh_json_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = claude_root(tmp)
            db = Path(tmp) / 'state' / 'h.sqlite3'
            roots = {'CLAUDE_PROJECTS': root, 'CODEX_SESSIONS': Path(tmp) / 'none', 'PI_SESSIONS': Path(tmp) / 'none',
                     'OPENCODE_DB': Path(tmp) / 'none.db'}
            out = io.StringIO()
            with patch.multiple('tokenatlas.why', **roots), redirect_stdout(out):
                self.assertEqual(main(['--db', str(db), 'overhead', '--refresh', '--json']), 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data['harnesses']['claude']['floor']['n'], 2)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(['--db', str(db), 'overhead', '--harness', 'claude']), 0)
            self.assertIn('estimate', out.getvalue())
            self.assertIn('pdf', out.getvalue())
            with redirect_stdout(io.StringIO()):
                code=main(['--db', str(Path(tmp) / 'missing.sqlite3'), 'overhead'])
            self.assertEqual(code, 3)


if __name__ == '__main__':
    unittest.main()
