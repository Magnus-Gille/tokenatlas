#!/usr/bin/env python3
"""Regenerate the TokenAtlas product-page demo: synthetic logs, a report and screenshots.

Usage: python3 scripts/demo.py OUTDIR [--seed N] [--no-screens] [--shared]

Everything is fictional. Logs are written under a temporary HOME in the exact on-disk formats the collectors
read (Claude Code, Codex, Pi, OpenCode), then the real `python3 -m tokenatlas` CLI runs against that HOME. No real
user logs or state are read. Stdlib only; Playwright is used only for the PNGs (skipped with --no-screens).
"""
import argparse
import base64
import gzip
import html
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYWRIGHT_DEFAULT = '/Users/magnus/.npm/_npx/705bc6b22212b352/node_modules/playwright'
PROJECTS = {'acme': '/Users/demo/code/acme-api', 'shop': '/Users/demo/code/webshop', 'docs': '/Users/demo/code/docs-site'}
WORDS = ('lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore '
         'magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo '
         'consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur').split()
ORCH = 'demo-orchestrated'
# Sessions where the fictional user stopped a turn: the logs then hold Claude Code's interrupt marker / Codex's turn_aborted event.
INTERRUPTED = {'demo-shop-02', 'cx-shop-long'}
# Fictional developer prompts per demo project; chosen deterministically from the seed (see Script).
PROMPTS = {  # project -> (short title, prompt, git branch derived from the title)
    'acme': [('Orders pagination', 'Add pagination to GET /orders and update the OpenAPI spec', 'feature/orders-pagination'),
             ('Auth middleware cleanup', 'Refactor the auth middleware to use the new token validator and keep the existing tests green', 'refactor/auth-middleware'),
             ('Invoice 500 error', 'The /invoices endpoint returns 500 when the customer has no address. Find the cause and fix it', 'fix/invoice-missing-address'),
             ('API rate limiting', 'Add rate limiting to the public API and document the limits in the README', 'feature/api-rate-limiting'),
             ('Users timezone column', 'Add a nullable timezone column to the users table with a reversible migration', 'feature/users-timezone-column'),
             ('Refund flow tests', 'Write integration tests for the refund flow, including the partial refund case', 'test/refund-flow'),
             ('Webhook retries', 'Webhook deliveries are not retried after a 503. Add exponential backoff with a retry cap', 'feature/webhook-retries'),
             ('Slow orders query', 'The orders list query takes seconds on large accounts. Find the missing index and add it', 'fix/orders-query-index'),
             ('Request logging', 'Add structured request logging with a correlation id and make sure no tokens end up in the logs', 'feature/request-logging'),
             ('Order status enum', 'Replace the stringly-typed order status with an enum and migrate the existing rows', 'refactor/order-status-enum'),
             ('Health endpoint', 'Add a /healthz endpoint that also checks the database connection and returns build info', 'feature/healthz-endpoint'),
             ('Python upgrade', 'Upgrade the service to the latest Python minor release and fix whatever breaks in CI', 'chore/python-upgrade')],
    'shop': [('Checkout test fixes', 'Fix the failing checkout tests in the webshop and explain what broke', 'fix/checkout-tests'),
             ('Wishlist button', 'Add a wishlist button to the product page and persist it for logged-in users', 'feature/wishlist-button'),
             ('Cart rounding bug', 'The cart total is off by one cent for discounted items. Track down the rounding bug', 'fix/cart-rounding'),
             ('Price formatter', 'Replace the hand-rolled price formatter with Intl.NumberFormat and update the snapshots', 'refactor/price-formatter'),
             ('Responsive product grid', 'Make the product grid responsive on small screens without changing the desktop layout', 'feature/responsive-grid'),
             ('Checkout skeleton', 'Add a loading skeleton to the checkout page while shipping options are fetched', 'feature/checkout-skeleton'),
             ('Coupon stacking', 'Customers can stack two percentage coupons. Make the rules explicit and add tests for each combination', 'fix/coupon-stacking'),
             ('Image lazy loading', 'Lazy-load product images below the fold and keep the layout from jumping', 'feature/image-lazy-loading'),
             ('Order emails', 'Move the order confirmation email to a template with a plain-text fallback', 'feature/order-email-template'),
             ('Search filters', 'Add size and colour filters to product search and keep them in the URL', 'feature/search-filters'),
             ('Stock badge', 'Show a low-stock badge when fewer than five items are left, without extra API calls', 'feature/low-stock-badge'),
             ('Address form a11y', 'The address form fails keyboard navigation. Fix the tab order and add proper labels', 'fix/address-form-a11y')],
    'docs': [('Slow docs build', 'Why is the docs build so slow? Profile it and suggest fixes', 'chore/docs-build-speed'),
             ('Offline docs search', 'Add a search page to the docs site that works without a backend', 'feature/offline-search'),
             ('Broken links', 'Fix the broken internal links reported by the link checker', 'fix/broken-links'),
             ('Getting-started rewrite', 'Rewrite the getting-started guide for the 2.0 API and add a quickstart snippet', 'docs/getting-started-2-0'),
             ('Docs dark mode', 'Add dark mode support to the docs theme and check the contrast ratios', 'feature/docs-dark-mode'),
             ('API reference build', 'Generate the API reference pages from the OpenAPI spec during the build', 'feature/api-reference-build'),
             ('Versioned docs', 'Add a version switcher so readers can move between the 1.x and 2.x docs', 'feature/version-switcher'),
             ('Code sample tests', 'Extract the code samples from the guides and run them in CI so they cannot rot', 'test/code-samples'),
             ('Sidebar navigation', 'Group the sidebar by task instead of by module and add a short intro to each group', 'docs/sidebar-by-task'),
             ('Image optimisation', 'Convert the screenshots to WebP at build time and add width and height attributes', 'chore/image-optimisation'),
             ('Changelog page', 'Generate a changelog page from the release notes and link it from the footer', 'docs/changelog-page'),
             ('Migration guide', 'Write a migration guide from 1.x to 2.x listing every breaking change with before and after code', 'docs/migration-guide')],
}
FOLLOWUPS = ['Also update the README', 'Run the tests again', 'Keep the change small, please', 'Add a changelog entry for this']
FINALS = ['Done. The change is in place and the tests pass; I also touched up the related docs.',
          'Fixed. The root cause was a missing null check, and I added a regression test for it.',
          'All green now. Summary: one small refactor, two new tests and no behaviour change elsewhere.',
          'Finished. I kept the diff small and left a note on the one edge case I could not cover.',
          'Implemented and verified locally. Next step would be a review of the naming in the new helper.']
SCOPES = ['Keep the change backwards compatible.', 'Start with the smallest change that works.', 'Add a test for it.',
          'Mention anything risky in the summary.', 'Do not touch unrelated files.', 'Follow the existing code style.',
          'Check the docs for anything that needs updating.', 'Explain your reasoning briefly as you go.']
PROGRESS = ['Reading the relevant files first.', 'Running the tests to see the current state.', 'Applying the change.',
            'Checking the edge cases.', 'Updating the tests to match.']


class Script:
    """Seeded, independent source of fictional prompt text so the token streams stay untouched by it."""

    def __init__(self, seed):
        self.rng = random.Random(f'prompts-{seed}')
        self.bag = {}
        self.sessions, self.fresh, self.rounds = {}, set(), {}

    def draw(self, key):
        """Next (title, prompt, branch) of the project's shuffled deck; the deck is refilled only once it is exhausted.
        A refilled round adds a distinct scope clause so a prompt never repeats verbatim."""
        if not self.bag.get(key):
            self.rounds[key] = self.rounds.get(key, -1) + 1
            self.bag[key] = self.rng.sample(PROMPTS[key], len(PROMPTS[key]))
        title, text, branch = self.bag[key].pop()
        if self.rounds[key]:
            text += ('' if text.endswith(('.', '?', '!')) else '.') + ' ' + SCOPES[(self.rounds[key] - 1) % len(SCOPES)]
        return title, text, branch

    def begin(self, key, sid):
        """Draw a session's first prompt (and its title and branch)."""
        if sid not in self.sessions:
            self.sessions[sid] = self.draw(key)
            self.fresh.add(sid)
        return self.sessions[sid]

    def prompt(self, key, sid):
        """(prompt text, git branch) of the next initiating input."""
        self.begin(key, sid)
        if sid in self.fresh:
            self.fresh.discard(sid)
            return self.sessions[sid][1:]
        return self.draw(key)[1:]

    def branch(self, key, sid):
        return self.begin(key, sid)[2]

    def title(self, key, sid):
        return self.begin(key, sid)[0]

    def followup(self):
        return self.rng.choice(FOLLOWUPS) if self.rng.random() < .35 else None

    def final(self):
        return self.rng.choice(FINALS)

    def progress(self):
        return self.rng.choice(PROGRESS)


def utc(day, hh, mm=0):
    return datetime(2026, 9, day, hh, mm, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def lorem(rng, chars):
    out, size = [], 0
    while size < chars:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return ' '.join(out)[:chars]


def hexid(rng, n):
    return ''.join(rng.choice('0123456789abcdef') for _ in range(n))


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


# ----- Claude Code -----------------------------------------------------------------------------------------

def attachments(rng, t, sid, cwd, common, main=True):
    def att(kind, **fields):
        return {'type': 'attachment', 'timestamp': iso(t), 'uuid': hexid(rng, 12), 'sessionId': sid, 'cwd': cwd,
                'attachment': {'type': kind, **fields}, **common}
    rows = [att('prompt_snapshot', systemPrompt=lorem(rng, rng.randint(24000, 30000)))]
    rows.append(att('instructions', files=[
        {'path': f'{cwd}/CLAUDE.md', 'content': lorem(rng, rng.randint(2500, 6000))},
        {'path': '/Users/demo/.claude/CLAUDE.md', 'content': lorem(rng, rng.randint(1500, 3500))}]))
    if main:
        count = rng.randint(14, 32)
        rows.append(att('skill_listing', skillCount=count, content=lorem(rng, count * rng.randint(220, 300))))
        rows.append(att('mcp_instructions_delta', addedNames=['github', 'playwright'],
                        addedBlocks=[lorem(rng, rng.randint(1500, 3000)), lorem(rng, rng.randint(1200, 2500))]))
        rows.append(att('deferred_tools_delta', addedNames=[f'tool_{i}' for i in range(12)],
                        addedLines=[lorem(rng, 90) for _ in range(12)]))
    return rows


def claude_thread(rng, t0, n, model, effort, sid, cwd, *, floor, gap, out_range=(180, 2200), entrypoint='cli',
                  agent=None, main=True, skill=None, prompt_every=7, script=None, key=None, branch='main'):
    """One Claude transcript: attachment rows, then n streamed assistant calls with a growing cached context."""
    common = {'entrypoint': entrypoint, 'version': '2.3.0', 'isSidechain': agent is not None, 'gitBranch': branch}
    if agent:
        common.update(agentId=agent[0], attributionAgent=agent[1])
    rows = attachments(rng, t0, sid, cwd, common, main)
    if script and not agent:
        rows.append({'type': 'custom-title', 'customTitle': script.title(key, sid), 'sessionId': sid})
    t, ctx = t0 + timedelta(seconds=1), 0
    pending = skill
    for i in range(n):
        t += timedelta(seconds=rng.randint(*gap))
        if i % prompt_every == 0 and not agent:
            if script:
                text, common['gitBranch'] = script.prompt(key, sid)
            else:
                text = lorem(rng, 80)
            rows.append({'type': 'user', 'timestamp': iso(t), 'uuid': hexid(rng, 12), 'sessionId': sid, 'cwd': cwd,
                         'message': {'role': 'user', 'content': text},
                         **common})
            t += timedelta(seconds=2)
        delta = floor if i == 0 else rng.randint(400, 7000) if rng.random() > .2 else rng.randint(9000, 16000)
        read, ctx = ctx, ctx + delta
        out = rng.randint(*out_range)
        split = ({'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': delta} if i == 0
                 else {'ephemeral_5m_input_tokens': delta, 'ephemeral_1h_input_tokens': 0})
        usage = {'input_tokens': rng.randint(1, 6), 'cache_creation_input_tokens': delta, 'cache_read_input_tokens': read,
                 'output_tokens': out, 'cache_creation': split, 'service_tier': 'standard', 'speed': 'standard'}
        last = i == n - 1 or (i + 1) % prompt_every == 0
        if script and not agent:
            text = script.final() if last else script.progress()
        else:
            text = lorem(rng, 60)
        content = [{'type': 'text', 'text': text}]
        if script and not agent and not last and rng.random() < .5:
            content.append({'type': 'tool_use', 'id': 'toolu_' + hexid(rng, 10), 'name': rng.choice(('Bash', 'Edit', 'Read')),
                            'input': {'command': 'python -m pytest -q', 'file_path': f'{cwd}/src/app.py'}})
        if pending and i == 3:
            content.append({'type': 'tool_use', 'id': 'toolu_' + hexid(rng, 10), 'name': 'Skill', 'input': {'skill': pending}})
        rows.append({'type': 'assistant', 'timestamp': iso(t), 'requestId': 'req_' + hexid(rng, 14), 'uuid': hexid(rng, 12),
                     'sessionId': sid, 'cwd': cwd, 'effort': effort, **common,
                     'message': {'id': 'msg_' + hexid(rng, 14), 'role': 'assistant', 'model': model,
                                 'stop_reason': 'tool_use' if i < n - 1 and rng.random() > .25 else 'end_turn',
                                 'usage': usage, 'content': content}})
        if script and not agent and sid in INTERRUPTED and i == 9:
            rows.append({'type': 'user', 'timestamp': iso(t + timedelta(seconds=1)), 'uuid': hexid(random.Random(f'{sid}-stop'), 12), 'sessionId': sid, 'cwd': cwd,
                         'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}, **common})
        if pending and i == 3:
            tool_id = content[-1]['id']
            rows.append({'type': 'user', 'isMeta': True, 'sourceToolUseID': tool_id, 'timestamp': iso(t + timedelta(seconds=1)),
                         'uuid': hexid(rng, 12), 'sessionId': sid, 'cwd': cwd,
                         'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content':
                                                             '---\nname: ' + pending + '\n---\n' + lorem(rng, rng.randint(4000, 7000))}]},
                         **common})
            pending = None
        ctx += out
    return rows, t


def subagent_path(base, sid, agent_id, wf=None):
    return base / sid / 'subagents' / (f'workflows/{wf}/' if wf else '') / f'agent-{agent_id}.jsonl'


def claude_session(w, rng, key, sid, t0, n, model, subs=(), skill=None, gap=(40, 200), script=None):
    """A main session plus subagents; subs are (offset_min, agent type, n_calls, workflow run or None)."""
    cwd = PROJECTS[key]
    base = w['claude'] / cwd.replace('/', '-')
    effort = 'high' if 'opus' in model else 'medium'
    rows, end = claude_thread(rng, t0, n, model, effort, sid, cwd, floor=rng.randint(21000, 27000), gap=gap, skill=skill,
                              script=script, key=key, branch=script.branch(key, sid) if script else 'main')
    write_jsonl(base / f'{sid}.jsonl', rows)
    ids = []
    for offset, kind, calls, wf in subs:
        agent_id = 'a' + hexid(rng, 16)
        sub_rows, sub_end = claude_thread(rng, t0 + timedelta(minutes=offset), calls, 'claude-sonnet-5-5', 'medium', sid, cwd,
                                          floor=rng.randint(9000, 14000), gap=(12, 45), agent=(agent_id, kind), main=False,
                                          out_range=(300, 3200))
        write_jsonl(subagent_path(base, sid, agent_id, wf), sub_rows)
        ids.append((agent_id, kind))
        end = max(end, sub_end)
    return ids, end


# ----- Codex -----------------------------------------------------------------------------------------------

SKILL_BODY = '---\nname: {name}\ndescription: demo skill\n---\n# {name}\n{text}'


def mark_demo(path):
    """Set `demo: true` in the report payload: "Open in Codex" and command copying then explain in a toast instead (every id, path and command
    in it is fictional); "Copy prompt" still copies the fictional prompt."""
    sys.path.insert(0, str(ROOT))
    from tokenatlas.report import pack
    page = path.read_text(encoding='utf-8')
    pattern = re.compile(r'(<script id="report-data" type="application/octet-stream\+base64">)([A-Za-z0-9+/=]+)(</script>)')
    def mark(m):
        data = json.loads(gzip.decompress(base64.b64decode(m.group(2))).decode('utf-8'))
        data['demo'] = True
        return m.group(1) + pack(data) + m.group(3)
    marked, n = pattern.subn(mark, page, count=1)
    if n != 1:
        raise SystemExit('demo report has no data block to mark')
    path.write_text(marked, encoding='utf-8')


def codex_id(name):
    """A fictional but UUID-shaped Codex thread id (the report only builds an Open-in-Codex link for real-looking ids)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://tokenatlas.invalid/demo/' + name))


def codex_rollout(w, rng, name, key, t0, n, models, *, kind='tui', parent=None, skill=None, gap=(30, 150), turn_len=6):
    sid, parent = name, parent and codex_id(parent)  # `name` seeds the script text; the logs carry the UUID
    uid = codex_id(name)
    cwd = PROJECTS[key]
    script = w['script']
    exec_run = kind == 'exec'
    source = ({'subagent': {'thread_spawn': {'parent_thread_id': parent, 'agent_nickname': kind, 'agent_role': 'worker'}}}
              if parent else 'exec' if exec_run else 'cli')
    stamp = t0.strftime('%Y-%m-%dT%H-%M-%S')
    path = w['codex'] / t0.strftime('%Y/%m/%d') / f'rollout-{stamp}-{uid}.jsonl'
    t = t0
    def row(ts, typ, payload, **extra):
        return {'timestamp': iso(ts), 'type': typ, 'payload': payload, **extra}
    rows = [row(t, 'session_meta', {'id': uid, 'timestamp': iso(t), 'cwd': cwd, 'model_provider': 'openai', 'source': source,
                                    'originator': 'codex_exec' if exec_run else 'codex-tui', 'cli_version': '0.9.2',
                                    'git': {'branch': script.branch(key, sid), 'repository_url': f'https://git.example.com/demo/{cwd.rsplit("/", 1)[1]}.git'},
                                    'base_instructions': {'text': lorem(rng, rng.randint(22000, 30000))}})]
    rows.append(row(t, 'response_item', {'type': 'message', 'role': 'developer', 'content': [
        {'type': 'input_text', 'text': '<permissions>' + lorem(rng, 600) + '</permissions>\n<skills_instructions>'
         + lorem(rng, rng.randint(5000, 9000)) + '</skills_instructions>'}]}))
    rows.append(row(t, 'response_item', {'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': f'# AGENTS.md instructions for {cwd}\n\n<INSTRUCTIONS>' + lorem(rng, rng.randint(3000, 6000)) + '</INSTRUCTIONS>\n<environment_context>'
         + lorem(rng, 300) + '</environment_context>'}]}))
    total, ctx, ordinal = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'), 0), 0, 0
    for i in range(n):
        t += timedelta(seconds=rng.randint(*gap))
        model = models[min(i * len(models) // n, len(models) - 1)]
        turn = f'turn-{sid}-{i // turn_len}'
        if i % turn_len == 0:
            rows.append(row(t, 'event_msg', {'type': 'task_started', 'turn_id': turn}))
            rows.append(row(t, 'event_msg', {'type': 'user_message', 'message': script.prompt(key, sid)[0]}))
            if (follow := script.followup()) and n - i > 3:
                rows.append(row(t, 'event_msg', {'type': 'item_completed', 'turn_id': turn, 'item': {
                    'type': 'UserMessage', 'id': f'item-{turn}', 'content': [{'type': 'text', 'text': follow}]}}))
        rows.append(row(t, 'turn_context', {'model': model, 'effort': rng.choice(('medium', 'high')), 'cwd': cwd}))
        if skill and i == 2:
            call = 'call_' + hexid(rng, 8)
            rows.append(row(t, 'response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': call,
                                                 'input': f'cat /Users/demo/.codex/skills/{skill}/SKILL.md'}))
            rows.append(row(t, 'response_item', {'type': 'custom_tool_call_output', 'call_id': call,
                                                 'output': SKILL_BODY.format(name=skill, text=lorem(rng, rng.randint(3000, 5000)))}))
        if rng.random() < .5:
            rows.append(row(t, 'response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call_' + hexid(rng, 8),
                                                 'arguments': json.dumps({'cmd': 'python -m pytest -q'})}))
        delta = rng.randint(12500, 16000) if i == 0 else rng.randint(300, 6000) if rng.random() > .2 else rng.randint(8000, 15000)
        prev, ctx = ctx, ctx + delta
        out = rng.randint(250, 2400)
        last = {'input_tokens': ctx, 'cached_input_tokens': prev, 'cache_write_input_tokens': 0, 'output_tokens': out,
                'reasoning_output_tokens': int(out * rng.uniform(.25, .6)), 'total_tokens': ctx + out}
        for name in total:
            total[name] += last[name]
        total_tokens = total['input_tokens'] + total['output_tokens']
        ordinal += 1
        rows.append(row(t, 'event_msg', {'type': 'token_count', 'info': {'last_token_usage': last,
                        'total_token_usage': {**total, 'total_tokens': total_tokens}}}, ordinal=ordinal))
        ctx += out
        if i % turn_len == turn_len - 1 or i == n - 1:
            rows.append(row(t, 'response_item', {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': script.final()}]}))
            rows.append(row(t, 'event_msg', {'type': 'turn_aborted' if sid in INTERRUPTED and i == n - 1 else 'task_complete', 'turn_id': turn}))
    write_jsonl(path, rows)
    with (w['codex'].parent / 'session_index.jsonl').open('a') as index:
        index.write(json.dumps({'id': uid, 'thread_name': script.title(key, sid), 'updated_at': iso(t)}) + '\n')
    return t


# ----- Pi and OpenCode -------------------------------------------------------------------------------------

def pi_session(w, rng, sid, key, t0, n, provider, model, turn_len=6):
    cwd = PROJECTS[key]
    script = w['script']
    rows = [{'type': 'session', 'version': 3, 'id': sid, 'timestamp': iso(t0), 'cwd': cwd},
            {'type': 'session_info', 'name': script.title(key, sid)}]
    t, ctx = t0, 0
    for i in range(n):
        t += timedelta(seconds=rng.randint(30, 160))
        if i % turn_len == 0:
            rows.append({'type': 'message', 'id': f'{sid}-u{i // turn_len}', 'timestamp': iso(t),
                         'message': {'role': 'user', 'content': [{'type': 'text', 'text': script.prompt(key, sid)[0]}]}})
            t += timedelta(seconds=2)
        delta = rng.randint(9000, 13000) if i == 0 else rng.randint(300, 5000)
        read, ctx = (0, ctx + delta) if i == 0 else (ctx, ctx + delta)
        out = rng.randint(200, 1800)
        usage = {'input': delta if i == 0 else rng.randint(20, 300), 'cacheRead': read, 'cacheWrite': 0 if i == 0 else delta,
                 'output': out, 'reasoning': int(out * .3)}
        usage['totalTokens'] = sum(usage[k] for k in ('input', 'cacheRead', 'cacheWrite', 'output'))
        rows.append({'type': 'message', 'id': f'{sid}-{i}', 'timestamp': iso(t),
                     'message': {'role': 'assistant', 'provider': provider, 'model': model, 'responseId': f'resp_{sid}_{i}',
                                 'usage': usage, 'content': [{'type': 'text', 'text': script.final() if i % turn_len == turn_len - 1 or i == n - 1 else script.progress()}]}})
        ctx += out
    write_jsonl(w['pi'] / f'{t0:%Y-%m-%dT%H-%M-%S}_{sid}.jsonl', rows)


def opencode_db(path, rng, script):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, directory TEXT NOT NULL,
            title TEXT, version TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT);""")
    specs = [('oc-webshop-1', None, 'shop', utc(9, 8, 5), 14, 'openai', 'gpt-5.6-terra', 'build', 5),
             ('oc-webshop-1-explore', 'oc-webshop-1', 'shop', utc(9, 8, 20), 8, 'openai', 'gpt-5.6-terra', 'explore', 5),
             ('oc-docs-1', None, 'docs', utc(19, 11, 30), 10, 'opencode', 'big-pickle', 'build', 5),
             # Local inference keeps the demo's client origin (OpenCode) while
             # exercising the separate local-provider classification in reports.
             ('oc-docs-local', None, 'docs', utc(20, 11, 30), 6, 'ollama', 'qwen3:8b', 'build', 3),
             # One long agentic turn on a larger model: a costly OpenCode turn.
             ('oc-shop-long', None, 'shop', utc(13, 9, 0), 48, 'openai', 'gpt-6-sol', 'build', 48)]
    for sid, parent, key, t0, n, provider, model, agent, turn_len in specs:
        cwd = PROJECTS[key]
        ms0 = int(t0.timestamp() * 1000)
        con.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?,?)',
                    (sid, 'demo-project-' + key, parent, cwd, script.title(key, sid), '1.18.32', ms0, ms0 + n * 90000))
        ctx, user, clock = 0, None, ms0
        for i in range(n):
            clock += rng.randint(40000, 110000)
            created = clock
            if i % turn_len == 0:
                user = f'{sid}-u{i // turn_len}'
                udata = {'role': 'user', 'agent': agent, 'time': {'created': created - 1000}}
                con.execute('INSERT INTO message VALUES (?,?,?,?,?)', (user, sid, created - 1000, created - 1000, json.dumps(udata)))
                text = {'type': 'text', 'text': script.prompt(key, sid)[0]}
                con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', (f'{user}-p', user, sid, created - 1000, created - 1000, json.dumps(text)))
            delta = rng.randint(11000, 15000) if i == 0 else rng.randint(300, 4500)
            read, ctx = ctx, ctx + delta
            out = rng.randint(200, 1600)
            tokens = {'input': delta if i == 0 else rng.randint(10, 200), 'output': out, 'reasoning': int(out * .25),
                      'cache': {'read': read, 'write': 0 if i == 0 else delta}}
            tokens['total'] = tokens['input'] + tokens['output'] + tokens['reasoning'] + read + tokens['cache']['write']
            data = {'role': 'assistant', 'providerID': provider, 'modelID': model, 'agent': agent, 'variant': 'high',
                    'parentID': user, 'time': {'created': created, 'completed': created + 4000}, 'path': {'cwd': cwd, 'root': cwd}, 'tokens': tokens}
            con.execute('INSERT INTO message VALUES (?,?,?,?,?)', (f'{sid}-m{i}', sid, created, created + 4000, json.dumps(data)))
            text = {'type': 'text', 'text': script.final() if i % turn_len == turn_len - 1 or i == n - 1 else script.progress()}
            con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', (f'{sid}-m{i}-t', f'{sid}-m{i}', sid, created + 3000, created + 3000, json.dumps(text)))
            ctx += out
        if sid == 'oc-webshop-1':
            skill = {'type': 'tool', 'tool': 'skill', 'state': {'input': {'name': 'brainstorming'}, 'output': lorem(rng, 2600)}}
            con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', (f'{sid}-p1', f'{sid}-m2', sid, ms0 + 200000, ms0 + 200000, json.dumps(skill)))
    con.commit()
    con.close()


# ----- Build the synthetic HOME ----------------------------------------------------------------------------

def reject_for_limit(w, sid, key, resets_after=timedelta(hours=2), retries=3):
    """Append a rejected request (the five-hour limit) with retries to the end of a session, as Claude Code logs it. Deterministic and
    drawn without the rng, so the rest of the demo history is unchanged."""
    path = w['claude'] / PROJECTS[key].replace('/', '-') / f'{sid}.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    last = max(datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')) for r in rows if r.get('type') == 'assistant')
    resets = int((last + resets_after).timestamp())
    zero = {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
    for i in range(retries):
        t = last + timedelta(seconds=20 + 25 * i)
        rows.append({'type': 'assistant', 'timestamp': iso(t), 'requestId': f'req_demolimit{i:02d}', 'uuid': f'demolimit{i:04d}',
                     'sessionId': sid, 'cwd': PROJECTS[key], 'error': 'rate_limit', 'isApiErrorMessage': True,
                     'entrypoint': 'cli', 'version': '2.3.0', 'isSidechain': False,
                     'message': {'id': f'msg_demolimit{i:02d}', 'role': 'assistant', 'model': '<synthetic>', 'usage': dict(zero),
                                 'content': [{'type': 'text', 'text': 'Rate limit reached.'}]},
                     'quotaLimits': {'status': 'rejected', 'resetsAt': resets, 'rateLimitType': 'five_hour', 'overageStatus': 'rejected'}})
    write_jsonl(path, rows)


def add_codex_quota(w, peak=88):
    """Give every demo Codex token_count event a synthetic weekly `rate_limits` snapshot (plan 'pro', whole percent) so the report shows the
    share-of-limit line and the limit windows table. The windows are contiguous weeks (they reset every seven days from 1 September 00:00 UTC);
    the percentage rises with each request's tokens, scaled so the busiest week ends at `peak` percent. A post-pass in time order over the
    finished rollouts: deterministic, and drawn without the rng, so the rest of the demo history is unchanged."""
    week = timedelta(days=7)
    anchor = datetime(2026, 9, 1, tzinfo=timezone.utc)
    events, files = [], {}
    for path in sorted(w['codex'].rglob('*.jsonl')):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for i, r in enumerate(rows):
            if r.get('type') == 'event_msg' and r['payload'].get('type') == 'token_count':
                events.append((r['timestamp'], str(path), i, r['payload']['info']['last_token_usage']['total_tokens']))
        files[str(path)] = rows
    events.sort()
    index = lambda ts: (datetime.fromisoformat(ts.replace('Z', '+00:00')) - anchor) // week
    spent = {}
    for ts, _, _, tokens in events:
        spent[index(ts)] = spent.get(index(ts), 0) + tokens
    scale = max(spent.values()) / peak
    running = {}
    for ts, path, i, tokens in events:
        n = index(ts)
        running[n] = running.get(n, 0) + tokens
        files[path][i]['payload']['rate_limits'] = {
            'limit_id': 'codex', 'plan_type': 'pro', 'rate_limit_reached_type': None,
            'secondary': {'used_percent': int(running[n] / scale), 'window_minutes': 10080, 'resets_at': int((anchor + (n + 1) * week).timestamp())}}
    for path, rows in files.items():
        write_jsonl(Path(path), rows)


def add_claude_quota(projects, path, week_peak=62, five_peak=85):
    """Write the demo's claude-quota.jsonl, as `tokenatlas statusline --record-quota` would: one reading after each request of a main Claude session
    whose whole-percent 5-hour or weekly value moved. The counters are account-wide running token totals per window (5-hour windows and weeks
    both anchored at 1 September 00:00 UTC), scaled so the busiest week ends at `week_peak` percent and the busiest 5-hour window at `five_peak`.
    A post-pass in time order over the finished transcripts: deterministic, no rng, so the rest of the demo history is unchanged."""
    anchor = datetime(2026, 9, 1, tzinfo=timezone.utc)
    spans = (('five_hour', timedelta(hours=5)), ('seven_day', timedelta(days=7)))
    events = []
    for file in sorted(Path(projects).glob('*/*.jsonl')):  # main sessions only: a subagent's transcript is one level deeper
        for line in file.read_text().splitlines():
            r = json.loads(line)
            u = (r.get('message') or {}).get('usage')
            if r.get('type') == 'assistant' and u and not r.get('error'):
                events.append((r['timestamp'], r['sessionId'], u['input_tokens'] + u['output_tokens'] + u['cache_creation_input_tokens']))
    events.sort()
    index = lambda ts, span: (datetime.fromisoformat(ts.replace('Z', '+00:00')) - anchor) // span
    totals = {k: {} for k, _ in spans}
    for ts, _, tokens in events:
        for k, span in spans:
            totals[k][index(ts, span)] = totals[k].get(index(ts, span), 0) + tokens
    scale = {k: max(totals[k].values()) / peak for (k, _), peak in zip(spans, (five_peak, week_peak))}
    running, last, lines = {k: {} for k, _ in spans}, None, []
    for ts, sid, tokens in events:
        now = {}
        for k, span in spans:
            n = index(ts, span)
            running[k][n] = running[k].get(n, 0) + tokens
            now[k] = dict(used_percent=int(running[k][n] / scale[k]), resets_at=iso(anchor + (n + 1) * span))
        values = {k: (v['used_percent'], v['resets_at']) for k, v in now.items()}
        if values != last:
            last = values
            lines.append(json.dumps(dict(ts=iso(datetime.fromisoformat(ts.replace('Z', '+00:00')) + timedelta(seconds=1)), session=sid, **now), sort_keys=True))
    Path(path).write_text('\n'.join(lines) + '\n')


def build_home(home, seed):
    rng = random.Random(seed)
    w = {'claude': home / '.claude/projects', 'codex': home / '.codex/sessions', 'pi': home / '.pi/agent/sessions',
         'script': Script(seed)}
    sonnet, opus = 'claude-sonnet-5-5', 'claude-opus-5-5'
    S = lambda offset, kind, calls, wf=None: (offset, kind, calls, wf)  # noqa: E731
    claude = [
        ('demo-acme-01', 'acme', utc(2, 7, 15), 55, sonnet, [], None),
        ('demo-acme-02', 'acme', utc(8, 6, 40), 70, opus, [S(20, 'Explore', 16)], 'brainstorming'),
        ('demo-acme-04', 'acme', utc(29, 8, 0), 38, sonnet, [], None),
        ('demo-shop-01', 'shop', utc(3, 12, 30), 62, opus, [S(30, 'implementer', 22)], None),
        ('demo-shop-02', 'shop', utc(10, 7, 0), 48, sonnet, [S(10, 'Explore', 12), S(25, 'implementer', 20)], None),
        ('demo-shop-03', 'shop', utc(16, 13, 10), 80, opus, [S(30, 'researcher', 14, 'wf_2f81c'), S(31, 'researcher', 13, 'wf_2f81c'),
                                                             S(60, 'implementer', 24)], 'writing-plans'),
        ('demo-shop-04', 'shop', utc(23, 6, 30), 40, sonnet, [], None),
        ('demo-docs-01', 'docs', utc(4, 9, 0), 30, sonnet, [], None),
        ('demo-docs-02', 'docs', utc(15, 8, 20), 36, sonnet, [S(8, 'Explore', 10)], None),
        ('demo-docs-03', 'docs', utc(21, 12, 0), 44, opus, [S(15, 'implementer', 18)], None),
        ('demo-docs-04', 'docs', utc(28, 7, 30), 28, sonnet, [], None),
    ]
    for sid, key, t0, n, model, subs, skill in claude:
        claude_session(w, rng, key, sid, t0, n, model, subs, skill, script=w['script'])
    reject_for_limit(w, 'demo-shop-03', 'shop')  # one five-hour limit hit (and two retries) at the end of the longest shop session
    # The showcase session: an opus conductor, three implementers, one Explore and an inferred headless Codex child.
    t0 = utc(24, 9, 5)
    subs = [(4, 'Explore', 14, None), (14, 'implementer', 26, None), (16, 'implementer', 30, None), (48, 'implementer', 34, None)]
    ids, _ = claude_session(w, rng, 'acme', ORCH, t0, 44, opus, subs, 'brainstorming', gap=(60, 220), script=w['script'])
    codex_rollout(w, rng, 'codex-orch-review', 'acme', t0 + timedelta(minutes=76), 22, ['gpt-6-sol'], kind='exec')
    # Other Codex rollouts, kept clear of the showcase window and cwd so only the review is inferred.
    luna, sol = 'gpt-5.6-luna', 'gpt-6-sol'
    codex = [('cx-acme-01', 'acme', utc(1, 8, 10), 40, [luna], 'tui', None, None),
             ('cx-shop-01', 'shop', utc(2, 9, 20), 55, [luna], 'tui', None, 'pdf'),
             ('cx-docs-01', 'docs', utc(7, 12, 0), 30, [luna], 'tui', None, None),
             ('cx-acme-02', 'acme', utc(9, 7, 30), 45, [sol], 'tui', None, None),
             ('cx-acme-02-worker', 'acme', utc(9, 7, 50), 14, [luna], 'Ada', 'cx-acme-02', None),
             ('cx-shop-02', 'shop', utc(11, 13, 0), 60, [luna], 'tui', None, None),
             ('cx-docs-02', 'docs', utc(14, 6, 50), 32, [luna], 'tui', None, None),
             ('cx-acme-03', 'acme', utc(17, 10, 40), 50, [sol], 'tui', None, None),
             ('cx-acme-03-worker', 'acme', utc(17, 11, 5), 16, [luna], 'Grace', 'cx-acme-03', None),
             ('cx-shop-03', 'shop', utc(18, 8, 0), 42, [luna], 'tui', None, None),
             ('cx-docs-03', 'docs', utc(22, 9, 30), 34, [sol, luna], 'tui', None, None),
             ('cx-acme-ci', 'acme', utc(25, 6, 0), 12, [luna], 'exec', None, None),
             ('cx-shop-ci', 'shop', utc(25, 11, 0), 14, [luna], 'exec', None, None),
             ('cx-docs-ci', 'docs', utc(29, 6, 20), 10, [luna], 'exec', None, None),
             # One long agentic turn on the larger model, as a migration the agent works through unattended.
             ('cx-shop-long', 'shop', utc(20, 9, 0), 42, [sol], 'tui', None, None, 42)]
    for sid, key, t0_, n, models, kind, parent, skill, *turn in codex:
        codex_rollout(w, rng, sid, key, t0_, n, models, kind=kind, parent=parent, skill=skill, **({'turn_len': turn[0]} if turn else {}))
    pi_session(w, rng, 'pi-acme-01', 'acme', utc(5, 8, 30), 26, 'openai-codex', luna)
    pi_session(w, rng, 'pi-shop-01', 'shop', utc(12, 9, 10), 32, 'openrouter', 'qwen/qwen3-coder')
    pi_session(w, rng, 'pi-docs-01', 'docs', utc(18, 7, 45), 20, 'openrouter', 'z-ai/glm-5.3')
    pi_session(w, rng, 'pi-acme-long', 'acme', utc(27, 8, 0), 52, 'openai-codex', sol, turn_len=52)
    pi_session(w, rng, 'pi-shop-02', 'shop', utc(26, 10, 0), 28, 'openai-codex', luna)
    opencode_db(home / '.local/share/opencode/opencode.db', rng, w['script'])
    add_codex_quota(w)
    return ids


def outcomes(path, agent_ids):
    (explore,), impl = [i for i, k in agent_ids if k == 'Explore'], [i for i, k in agent_ids if k == 'implementer']
    ag = lambda i: {'harness': 'claude', 'agent_id': i}  # noqa: E731
    units = [('map-endpoints', [ag(explore), ag(impl[0])], 'pass', 'Endpoint inventory and first handler port'),
             ('migrate-schema', [ag(impl[1])], 'pass', 'Migration applied and tests green'),
             ('rewrite-auth-tests', [ag(impl[2])], 'partial', 'Two flaky cases left'),
             ('cross-model-review', [{'harness': 'codex', 'session': codex_id('codex-orch-review')}], 'pass', 'Review found no blockers')]
    lines = [{'v': 1, 'root_session': ORCH, 'unit': u, 'threads': t, 'outcome': o, 'note': n, 'ts': '2026-09-24T12:30:00Z'}
             for u, t, o, n in units]
    path.write_text(''.join(json.dumps(x, sort_keys=True) + '\n' for x in lines))
    path.chmod(0o600)


# ----- Run the CLI -----------------------------------------------------------------------------------------

def cli(env, db, *args):
    proc = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(db), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit(f'tokenatlas {" ".join(args)} failed ({proc.returncode}):\n{proc.stderr}{proc.stdout}')
    return proc.stdout


def terminal_html(title, text):
    return f"""<!doctype html><meta charset="utf-8"><style>
html{{background:#0b1020}}html,body{{margin:0;background:#0b1020;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
body{{padding:28px;width:1144px;min-height:644px;display:flex;flex-direction:column;justify-content:center}}
.win{{background:#11172b;border:1px solid #2a3350;border-radius:12px;overflow:hidden;box-shadow:0 12px 40px #0008}}
.bar{{display:flex;gap:8px;align-items:center;padding:11px 14px;background:#1a2138;border-bottom:1px solid #2a3350;color:#8e9ac0;font-size:12px}}
.dot{{width:12px;height:12px;border-radius:50%}}
pre{{margin:0;padding:16px 18px;color:#d5dcf2;font-size:11.5px;line-height:1.5;white-space:pre}}
</style><div class="win" id="win"><div class="bar"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span style="margin-left:10px">{html.escape(title)}</span></div>
<pre>{html.escape(text)}</pre></div>"""


def trim_session(text):
    """Cost caveat, tree, per-model table and efficiency; drops the coordination line."""
    lines = text.split('\n')
    keep = lines[2:]
    out, skip = [], False
    for l in keep:
        if l.startswith('Coordination'):
            skip = True
        elif skip and l.startswith(('Unassigned', 'Efficiency')):
            skip = False
        if not skip:
            out.append(l)
    return '$ tokenatlas session ' + ORCH + '\n' + lines[0] + '\n' + '\n'.join(out).rstrip()


def trim_overhead(text):
    lines = text.split('\n')
    end = next((i for i, l in enumerate(lines) if l.startswith('Fixed context overhead')), len(lines))
    table = [l for l in lines[2:end] if l.strip() and not l.startswith(('excluded', 'claude subagents'))]
    detail, seen = [], 0
    for l in lines[end + 1:]:
        if re.match(r'^(claude|codex|pi|opencode):', l):
            seen += 1
            if seen > 2:
                break
        if seen and (re.match(r'^(claude|codex):', l) or l.startswith('  ') and 'recurring' not in l):
            detail.append(l)
    return '$ tokenatlas overhead --refresh\n' + '\n'.join(table) + '\n\n' + '\n'.join(detail).rstrip()


def run_screens(report, shots, outdir, work):
    module = os.environ.get('PLAYWRIGHT_MODULE') or PLAYWRIGHT_DEFAULT
    env = {**os.environ, 'PLAYWRIGHT_MODULE': module}
    cmd = ['node', str(ROOT / 'scripts/demo_screens.cjs'), str(report), str(outdir)] + [str(s) for s in shots]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=work)
    if proc.returncode:
        raise SystemExit(f'screenshots failed:\n{proc.stderr}{proc.stdout}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('outdir', type=Path)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--no-screens', action='store_true', help='Skip the Playwright screenshots.')
    ap.add_argument('--shared', action='store_true', help='Redact project labels and session ids in the HTML report (default keeps them; the data is fictional).')
    args = ap.parse_args(argv)
    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tokenatlas-demo-') as tmp:
        tmp = Path(tmp)
        home, state = tmp / 'home', tmp / 'state'
        home.mkdir()
        (state / 'tokenatlas').mkdir(parents=True)
        ids = build_home(home, args.seed)
        db, outc = state / 'tokenatlas' / 'history.sqlite3', state / 'tokenatlas' / 'outcomes.jsonl'
        outcomes(outc, ids)
        quota_file = state / 'tokenatlas' / 'claude-quota.jsonl'
        add_claude_quota(home / '.claude/projects', quota_file)  # the demo user records quota snapshots (the statusline default)
        # USERPROFILE is what Path.home() reads on Windows; SYSTEMROOT is needed there by Python itself.
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
               'XDG_STATE_HOME': str(state), 'TZ': 'Europe/Stockholm', 'PYTHONDONTWRITEBYTECODE': '1',
               **{k: os.environ[k] for k in ('SYSTEMROOT',) if k in os.environ}}
        for harness in ('claude', 'codex', 'pi', 'opencode'):
            cli(env, db, 'refresh', '--harness', harness)
        overhead_text = cli(env, db, 'overhead', '--refresh')
        session_text = cli(env, db, 'session', ORCH, '--outcomes', str(outc))
        session_json = json.loads(cli(env, db, 'session', ORCH, '--outcomes', str(outc), '--json'))
        cli(env, db, 'top', '--keep-text')
        # A manually configured Claude plan is deliberately explicit in the
        # demo, since imported history does not provide a documented plan field.
        cli(env, db, 'plan', 'set', '--harness', 'claude', 'max-5x')
        report = outdir / 'demo-report.html'
        report_args = ['report', '--html', str(report)] + ([] if args.shared else ['--private'])
        cli(env, db, *report_args)
        mark_demo(report)
        (outdir / 'session.txt').write_text(session_text + '\n')
        (outdir / 'overhead.txt').write_text(overhead_text + '\n')
        sys.path.insert(0, str(ROOT))
        from tokenatlas import limits, pricing, prompts, quota_share
        from tokenatlas.history import History
        counts = {}
        with History(db) as history:
            history.connection.execute('BEGIN')
            records = history.records()
            # The same ranking `tokenatlas top` and the report's Costliest turns card use.
            top = prompts.top_prompts(records, pricing.load_prices(), 10)['prompts']
            hits = limits.limit_hits(records, history.limit_events(), pricing.load_prices())
            snapshots, shares = quota_share.compute(records, pricing.load_prices(), claude=quota_file)  # the largest window per turn
            for item in records:
                slot = counts.setdefault(item['harness'], {'observations': 0, 'sessions': set(), 'first': item['ts'], 'last': item['ts']})
                slot['observations'] += 1
                slot['sessions'].add(item['session'])
                slot['first'], slot['last'] = min(slot['first'], item['ts']), max(slot['last'], item['ts'])
        nodes = []
        def walk(node):
            nodes.append(node)
            for child in node['children']:
                walk(child)
        walk(session_json['root'])
        summary = {'seed': args.seed, 'harnesses': {h: {'observations': c['observations'], 'sessions': len(c['sessions']), 'first': c['first'], 'last': c['last']}
                                                    for h, c in sorted(counts.items())},
                   'session': {'id': ORCH, 'subagent_nodes': sum(n['kind'] == 'subagent' for n in nodes),
                               'workflow_nodes': sum(n['kind'] == 'workflow' for n in nodes),
                               'inferred_children': sum(n['link'] == 'inferred' for n in nodes),
                               'total_tokens': session_json['total']['total'], 'cost': session_json['total']['cost']},
                   'top_turns': [{'rank': i, 'harness': p['harness'], 'project': p['project_label'], 'cost': p['cost'],
                                  'requests': p['requests'], 'subagents': p['subagents'], 'interrupted': p['interrupted']} for i, p in enumerate(top, 1)],
                   'limit_hits': [{'harness': h['harness'], 'reached': h['reached'], 'window_minutes': h['window_minutes'], 'retries': h['retries'],
                                   'window_cost': h['window'] and h['window']['cost']} for h in hits],
                   'quota_windows': [{'harness': q['harness'], 'minutes': q['minutes'], 'peak_percent': q['peak_percent'], 'hit': q['hit']}
                                     for q in quota_share.windows(snapshots)],
                   'quota_share_labels': {k: sum(1 for x in shares.values() if x['label'] == k) for k in ('observed', 'estimate', 'unknown')},
                   'claude_quota_snapshots': len(quota_file.read_text().splitlines()),
                   'report_features': {'manual_claude_plan': 'max-5x', 'local_provider': 'ollama',
                                      'unknown_project_or_branch': True, 'retained_top_k_only': True},
                   'report': str(report)}
        (outdir / 'demo-summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
        if not args.no_screens:
            shots = tmp / 'shots'
            shots.mkdir()
            (shots / 'session.html').write_text(terminal_html('tokenatlas session', trim_session(session_text)))
            (shots / 'overhead.html').write_text(terminal_html('tokenatlas overhead', trim_overhead(overhead_text)))
            run_screens(report, [shots / 'session.html', shots / 'overhead.html'], outdir, tmp)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
