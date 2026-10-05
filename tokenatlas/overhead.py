"""Fixed context overhead: sizes and floors of what every call re-reads (system prompt, instruction files, skills).

Privacy: only character counts, component/skill names, counts, session ids, timestamps and token counts are
stored, never content.  Instruction-file paths are kept as basename plus a stable hash of the full path.
Tables live in the history database but are owned here; they are rebuilt per session by rescanning (rescan-all
with an upsert per harness+session, no per-file fingerprint), so a rescan of unchanged logs is idempotent.
"""
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import progress

CHARS_PER_TOKEN = 4
HARNESSES = ('claude', 'codex', 'pi', 'opencode')
RESIDUAL_LABEL = 'other: tools, first prompt, unlogged'
COST_LABEL = 'API-equivalent at list price, estimate'
NOTE = ("Relative measures are usage-normalized. Token counts come from each provider's own tokenizer (Claude"
        ' 4.7+ counts about 30% more tokens for the same text), so compare percentages across providers rather than raw tokens.')
_DDL = (
    'CREATE TABLE IF NOT EXISTS overhead_sessions (harness TEXT NOT NULL, session TEXT NOT NULL, is_subagent INTEGER,'
    ' first_ts TEXT, floor_tokens INTEGER, floor_input INTEGER, floor_cache_write INTEGER, floor_cache_read INTEGER,'
    ' calls INTEGER, unidentified_skill_reads INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(harness, session))',
    'CREATE TABLE IF NOT EXISTS overhead_items (harness TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL,'
    ' name TEXT NOT NULL, chars INTEGER NOT NULL, source TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS overhead_skill_uses (harness TEXT NOT NULL, session TEXT NOT NULL, name TEXT NOT NULL,'
    ' chars INTEGER NOT NULL, ts TEXT, source TEXT NOT NULL)',
)
_SKILLS = re.compile(r'<skills_instructions>.*?</skills_instructions>', re.S)
_AGENTS = re.compile(r'<INSTRUCTIONS>.*?</INSTRUCTIONS>', re.S)
_ENV = re.compile(r'<environment_context>.*?</environment_context>', re.S)
_SKILL_PATH = re.compile(r'''(?:~|/)[^\s'"`]*?/skills/(?P<name>[A-Za-z0-9][A-Za-z0-9._:-]{0,80})/SKILL\.md''')
_GUTTER = re.compile(r'(?m)^[ \t]*\d+[\t\u2192]')
_FRONT_NAME = re.compile(r'(?m)^name:[ \t]*[\'"]?([A-Za-z0-9][A-Za-z0-9._:-]{0,80})[\'"]?[ \t]*$')
_HEADING = re.compile(r'#{1,6}[ \t]+\S')
_MIN_BODY = 200
_AGENT_FILE = re.compile(r'(?:^|/)agent-([^/]+)\.jsonl$')


def _skill_paths(command):
    """Names of concrete `.../skills/<name>/SKILL.md` paths in a command; [] when any mention is not one.

    A path inside a glob, brace expansion, variable or format pattern (`{ } * $` in its token) is rejected."""
    names = []
    for m in _SKILL_PATH.finditer(command):
        start = max(command.rfind(c, 0, m.start()) for c in ' \t\r\n\'"`') + 1
        ends = [i for i in (command.find(c, m.end()) for c in ' \t\r\n\'"`') if i >= 0]
        token = command[start:min(ends) if ends else len(command)]
        if any(c in token for c in '{}*$'):
            return []
        names.append(m.group('name'))
    return names if names and command.count('SKILL.md') == len(names) else []


def _chunks(output):
    """Text chunks of an exec output without its `Script ... Wall time ... Output:` header.

    A list output is the `text` of its input_text/output_text items; a leading item starting with `Script` and
    holding a `Wall time` line is the header.  A string is one chunk after the first line equal to `Output:`."""
    if isinstance(output, str):
        lines = output.split('\n')
        for i, line in enumerate(lines):
            if line.strip() == 'Output:':
                return ['\n'.join(lines[i + 1:])]
        return [output]
    if not isinstance(output, list):
        return []
    texts = [str(v.get('text') or '') for v in output
             if isinstance(v, dict) and v.get('type') in ('input_text', 'output_text')]
    if texts and texts[0].startswith('Script') and re.search(r'(?m)^Wall time\b', texts[0]):
        texts = texts[1:]
    return texts


def _skill_body(output):
    """(is_body, frontmatter name or None) for a chunk that should be a SKILL.md body."""
    if not isinstance(output, str) or len(output) < _MIN_BODY:
        return False, None
    text = output.lstrip()
    if _GUTTER.match(text):
        text = _GUTTER.sub('', text).lstrip()
    if text.startswith('---'):
        head = text[3:].split('\n---', 1)
        found = _FRONT_NAME.search(head[0]) if len(head) == 2 else None
        return (True, found.group(1)) if found else (False, None)
    return (True, None) if _HEADING.match(text) else (False, None)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text_len(value):
    """Characters of a string, or of the text parts of a content list; content is measured, never kept."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_len(v.get('text') if isinstance(v, dict) else v) for v in value)
    return 0 if value is None else len(json.dumps(value))


def _named(path):
    """Basename plus a stable hash of the full path; the directory itself is never stored."""
    return f'{Path(str(path)).name}#{hashlib.sha256(str(path).encode()).hexdigest()[:10]}'


def _rows(path):
    try:
        handle = open(path, encoding='utf-8', errors='replace')
    except OSError as exc:
        print(f'overhead: cannot read {path.name}: {exc}', file=sys.stderr)
        return
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _session(harness, session, sub=False):
    return {'harness': harness, 'session': session, 'is_subagent': sub, 'first_ts': None, 'floor_tokens': None,
            'floor_breakdown': None, 'calls': 0, 'components': [], 'skill_uses': [], 'unidentified_skill_reads': 0}


def _floor(row, inp, write, read):
    if row['floor_tokens'] is None and inp + write + read > 0:
        row['floor_tokens'] = inp + write + read
        row['floor_breakdown'] = {'input': inp, 'cache_write': write, 'cache_read': read}


def _item(row, kind, name, chars, source='exact'):
    row['components'].append({'kind': kind, 'name': name, 'chars': chars, 'source': source})


def _dict(value):
    return value if isinstance(value, dict) else {}


def _claude_attachment(row, a):
    kind = a.get('type')
    names = [str(n) for n in a.get('addedNames') or ()]
    if kind == 'skill_listing':
        _item(row, 'skill_listing', f"{_int(a.get('skillCount'))} skills", _text_len(a.get('content')))
    elif kind == 'instructions':
        for f in a.get('files') or ():
            if isinstance(f, dict):
                _item(row, 'instructions', _named(f.get('path')), _text_len(f.get('content')))
    elif kind == 'nested_memory':
        _item(row, 'nested_memory', _named(a.get('path')), _text_len(a.get('content')))
    elif kind == 'mcp_instructions_delta':
        _item(row, 'mcp_instructions', ','.join(names), _text_len(a.get('addedBlocks')))
    elif kind in ('deferred_tools_delta', 'deferred_tools_record'):
        _item(row, 'deferred_tools', ','.join(names), _text_len(a.get('addedLines')))
    elif kind == 'agent_listing_delta':
        _item(row, 'agent_listing', ','.join(str(n) for n in a.get('addedTypes') or ()), _text_len(a.get('addedLines')))
    elif kind == 'prompt_snapshot':
        _item(row, 'system_prompt', 'system_prompt', _text_len(a.get('systemPrompt')))
    elif kind == 'session_context':
        _item(row, 'session_context', 'session_context', _text_len(a.get('context')))


def _scan_claude(root):
    out = []
    for path in sorted(Path(root).rglob('*.jsonl')):
        parts = path.as_posix().replace('\\', '/').split('/')
        sub = 'subagents' in parts
        parent = parts[parts.index('subagents') - 1] if sub else None
        row = _session('claude', f'{parent}/{path.stem}' if sub else path.stem, sub)
        requests, uses, pending = set(), [], {}
        for r in _rows(path):
            row['first_ts'] = row['first_ts'] or r.get('timestamp')
            kind, message = r.get('type'), _dict(r.get('message'))
            if kind == 'attachment':
                _claude_attachment(row, _dict(r.get('attachment')))
            elif kind == 'assistant':
                usage = message.get('usage')
                if isinstance(usage, dict):
                    requests.add(r.get('requestId') or message.get('id') or f'row{len(requests)}')
                    _floor(row, _int(usage.get('input_tokens')), _int(usage.get('cache_creation_input_tokens')),
                           _int(usage.get('cache_read_input_tokens')))
                for block in message.get('content') if isinstance(message.get('content'), list) else ():
                    if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Skill':
                        pending[block.get('id')] = str(_dict(block.get('input')).get('skill') or 'unknown')
            elif kind == 'user' and r.get('isMeta') and r.get('sourceToolUseID') in pending:
                row['skill_uses'].append({'name': pending.pop(r['sourceToolUseID']), 'chars': _text_len(message.get('content')),
                                          'ts': r.get('timestamp'), 'source': 'exact'})
        row['calls'] = len(requests)
        if row['floor_tokens'] is not None or row['components'] or row['skill_uses']:
            out.append(row)
    return out


def _scan_codex(root):
    out = []
    for path in sorted(Path(root).rglob('rollout-*.jsonl')):
        row = _session('codex', path.stem)
        totals, calls, reads = [], {}, []
        for r in _rows(path):
            p = _dict(r.get('payload'))
            row['first_ts'] = row['first_ts'] or p.get('timestamp') or r.get('timestamp')
            kind = r.get('type')
            if kind == 'session_meta':
                row['session'] = str(p.get('id') or row['session'])
                row['is_subagent'] = isinstance(_dict(p.get('source')).get('subagent'), dict)
                _item(row, 'system_prompt', 'system_prompt', _text_len(_dict(p.get('base_instructions')).get('text')))
            elif kind == 'response_item' and p.get('type') == 'message':
                text = '\n'.join(str(_dict(c).get('text') or '') for c in p.get('content') or ())
                if p.get('role') == 'developer':
                    for m in _SKILLS.findall(text):
                        _item(row, 'skills_listing', 'skills_listing', len(m))
                elif p.get('role') == 'user':
                    for pattern, name in ((_AGENTS, 'agents_md'), (_ENV, 'environment_context')):
                        for m in pattern.findall(text):
                            _item(row, name, name, len(m))
            elif kind == 'response_item' and p.get('type') == 'custom_tool_call' and p.get('name') == 'exec':
                command = str(p.get('input') or '')
                if 'SKILL.md' in command:
                    calls[p.get('call_id')] = (_skill_paths(command), r.get('timestamp'))
            elif kind == 'response_item' and p.get('type') == 'custom_tool_call_output' and p.get('call_id') in calls:
                names, ts = calls.pop(p['call_id'])
                good = [(len(c), _skill_body(c)[1]) for c in _chunks(p.get('output')) if _skill_body(c)[0]]
                if not names or not good:
                    row['unidentified_skill_reads'] += 1
                    continue
                if len(names) == 1:
                    size, declared = next((g for g in good if g[1] == names[0]), good[0])
                    sized = [(declared or names[0], size)]
                else:  # several concrete paths: match chunks by frontmatter name, else split equally
                    by_name = {}
                    for size, declared in good:
                        by_name.setdefault(declared, size)
                    if all(n in by_name for n in names):
                        sized = [(n, by_name[n]) for n in names]
                    else:
                        sized = [(n, sum(g[0] for g in good) // len(names)) for n in names]
                for name, size in sized:
                    row['skill_uses'].append({'name': name, 'chars': size, 'ts': ts, 'source': 'heuristic'})
            elif kind == 'event_msg' and p.get('type') == 'token_count' and isinstance(p.get('info'), dict):
                info = p['info']
                last = _dict(info.get('last_token_usage'))
                total = _dict(info.get('total_token_usage')).get('total_tokens')
                if last and (total is None or total not in totals):
                    totals.append(total)
                    total_in, cached = _int(last.get('input_tokens')), _int(last.get('cached_input_tokens'))
                    if row['floor_tokens'] is None:
                        _floor(row, total_in - min(cached, total_in), 0, min(cached, total_in))
        row['calls'] = len(totals)
        if row['floor_tokens'] is not None or row['components'] or row['skill_uses'] or row['unidentified_skill_reads']:
            out.append(row)
    return out


def _scan_pi(root):
    out = []
    for path in sorted(Path(root).rglob('*.jsonl')):
        row, seen = _session('pi', path.stem), set()
        for r in _rows(path):
            if r.get('type') == 'session':
                row['session'] = str(r.get('id') or row['session'])
                row['first_ts'] = r.get('timestamp')
            usage = _dict(_dict(r.get('message')).get('usage'))
            if r.get('type') == 'message' and _dict(r.get('message')).get('role') == 'assistant' and usage:
                inp, write, read = _int(usage.get('input')), _int(usage.get('cacheWrite')), _int(usage.get('cacheRead'))
                if inp + write + read > 0:
                    row['first_ts'] = row['first_ts'] or r.get('timestamp')
                    seen.add(_dict(r.get('message')).get('responseId') or r.get('id') or f'row{len(seen)}')
                    _floor(row, inp, write, read)
        row['calls'] = len(seen)
        if row['floor_tokens'] is not None:
            out.append(row)
    return out


def _ms(value):
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(value) % 1000:03d}Z'
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _scan_opencode(root):
    try:
        con = sqlite3.connect(Path(root).expanduser().resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as exc:
        print(f'overhead: cannot open opencode database: {exc}', file=sys.stderr)
        return []
    rows = {}
    try:
        con.execute('PRAGMA query_only=ON')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        parents = dict(con.execute('SELECT id, parent_id FROM session')) if 'session' in tables else {}
        for sid, created, data in con.execute('SELECT session_id, time_created, data FROM message ORDER BY time_created, id'):
            try:
                d = json.loads(data)
            except (TypeError, ValueError):
                continue
            row = rows.setdefault(sid, _session('opencode', sid, parents.get(sid) is not None))
            row['first_ts'] = row['first_ts'] or _ms(created)
            tokens = _dict(_dict(d).get('tokens'))
            if _dict(d).get('role') == 'assistant' and tokens:
                cache = _dict(tokens.get('cache'))
                inp, read, write = _int(tokens.get('input')), _int(cache.get('read')), _int(cache.get('write'))
                if inp + read + write > 0:
                    row['calls'] += 1
                    _floor(row, inp, write, read)
        if 'part' in tables:
            for sid, created, data in con.execute('SELECT session_id, time_created, data FROM part ORDER BY time_created, id'):
                try:
                    d = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if _dict(d).get('type') == 'tool' and d.get('tool') == 'skill':
                    state = _dict(d.get('state'))
                    row = rows.setdefault(sid, _session('opencode', sid, parents.get(sid) is not None))
                    row['skill_uses'].append({'name': str(_dict(state.get('input')).get('name') or 'unknown'),
                                              'chars': _text_len(state.get('output')), 'ts': _ms(created), 'source': 'exact'})
    finally:
        con.close()
    return [r for r in rows.values() if r['floor_tokens'] is not None or r['skill_uses']]


def scan(harness, root):
    """Session dicts for one harness root.  `calls` counts distinct priced requests (Claude requestId, else
    message id; Codex distinct cumulative totals; Pi responseId/entry id; OpenCode assistant messages with tokens)."""
    return {'claude': _scan_claude, 'codex': _scan_codex, 'pi': _scan_pi, 'opencode': _scan_opencode}[harness](root)


def ensure(db):
    for sql in _DDL:
        db.execute(sql)
    if 'unidentified_skill_reads' not in {r[1] for r in db.execute('PRAGMA table_info(overhead_sessions)')}:
        db.execute('ALTER TABLE overhead_sessions ADD COLUMN unidentified_skill_reads INTEGER NOT NULL DEFAULT 0')


def save(db, harness, sessions):
    """Upsert sessions by harness+session; a session's items and skill uses are replaced wholesale."""
    if not db.in_transaction:
        db.execute('BEGIN')
    ensure(db)
    for s in sessions:
        key = (harness, s['session'])
        b = s['floor_breakdown'] or {}
        for table in ('overhead_items', 'overhead_skill_uses'):
            db.execute(f'DELETE FROM {table} WHERE harness=? AND session=?', key)
        db.execute('INSERT OR REPLACE INTO overhead_sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (*key, int(s['is_subagent']), s['first_ts'], s['floor_tokens'], b.get('input'), b.get('cache_write'),
                    b.get('cache_read'), s['calls'], s.get('unidentified_skill_reads', 0)))
        db.executemany('INSERT INTO overhead_items VALUES (?,?,?,?,?,?)',
                       [(*key, c['kind'], c['name'], c['chars'], c['source']) for c in s['components']])
        db.executemany('INSERT INTO overhead_skill_uses VALUES (?,?,?,?,?,?)',
                       [(*key, u['name'], u['chars'], u['ts'], u['source']) for u in s['skill_uses']])
    db.commit()


def _pct(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _moment(text):
    if not text:
        return None
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _slashed(path):
    """Imported snapshots may carry Windows source paths; match both separators."""
    return path.replace('\\', '/')


def _dominant_tariff(records, models):
    """Per (harness, session key): (most common tariff among the dominant model's records, whether tariffs differ)."""
    seen = {}
    for r in records:
        key = (r.get('harness'), str(r.get('session')).split('/')[0])
        if models.get(key) == (r.get('provider'), r.get('model')):
            label = json.dumps(r.get('tariff'), sort_keys=True)
            seen.setdefault(key, {}).setdefault(label, [0, r.get('tariff')])[0] += 1
    best = {k: max(sorted(v), key=lambda label: v[label][0]) for k, v in seen.items()}
    return {k: (v[best[k]][1], len(v) > 1) for k, v in seen.items()}


def _add_cost(total, cost, currency):
    """Add one priced amount to a {currency: amount} map; None (any unpriced part) stays None."""
    if total is None or cost is None:
        return None
    total[currency or '?'] = total.get(currency or '?', 0.0) + cost
    return total


def _dominant(records):
    counts = {}
    for r in records:
        key = (r.get('harness'), str(r.get('session')).split('/')[0])
        model = counts.setdefault(key, {})
        model[(r.get('provider'), r.get('model'))] = model.get((r.get('provider'), r.get('model')), 0) + 1
    return {k: max(sorted(v, key=str), key=v.get) for k, v in counts.items()}


def _joined(records, table):
    """Per (harness, session key) history totals: input, output, cost (None when any observation lacks it),
    whether output is a lower bound.  Claude subagent rows are keyed by session and agent file id (else agent field) apart from the main rows."""
    from tokenatlas import pricing
    out = {}
    for r in records:
        sub = r.get('harness') == 'claude' and r.get('thread_kind') == 'subagent'
        if sub:  # the transcript file name carries the agent id; the agent field is a type, kept as a fallback
            found = next((m.group(1) for m in map(_AGENT_FILE.search, map(_slashed, r.get('sources') or ())) if m), None)
            key = (r.get('harness'), (str(r.get('session')), 'file', found) if found
                   else (str(r.get('session')), 'agent', str(r.get('agent'))))
        else:
            key = (r.get('harness'), str(r.get('session')))
        t = r.get('tokens') or {}
        cls = [t.get(k) for k in ('fresh_input', 'cache_write', 'cache_read')]
        e = out.setdefault(key, {'input': 0, 'output': 0, 'cost': {}, 'lower': False})
        e['input'] = None if e['input'] is None or None in cls else e['input'] + sum(cls)
        e['output'] = None if e['output'] is None or t.get('output') is None else e['output'] + t['output']
        priced = pricing.price_observation(r, table) if table is not None else {'cost': None}
        e['cost'] = _add_cost(e['cost'], priced['cost'], priced.get('currency'))
        e['lower'] = e['lower'] or 'output_not_final' in (r.get('warnings') or ())
    return out


def _lookup(joined, h, s, sub):
    """History totals for an overhead session; (totals, reason for exclusion)."""
    if h == 'claude' and sub:
        parent, _, stem = s.partition('/')
        ident = stem[6:] if stem.startswith('agent-') else stem
        for key in ((parent, 'file', ident), (parent, 'agent', stem), (parent, 'agent', ident)):
            if (h, key) in joined:
                return joined[(h, key)], None
        return None, 'subagent_unmatched'
    found = joined.get((h, s.split('/')[0] if h == 'claude' else s))
    return found, None if found else 'no_history_records'


def _share(num, den, scale=100):
    return num / den * scale if den else None


def _cost_shares(pairs):
    """Fixed share of cost per currency over (recurring map, total map) pairs; only currencies with a total."""
    currencies = sorted({c for _, total in pairs for c in total})
    shares = {c: _share(sum(f.get(c, 0.0) for f, _ in pairs), sum(t.get(c, 0.0) for _, t in pairs)) for c in currencies}
    return {c: v for c, v in shares.items() if v is not None}


def _relative(h, mine, joined, rec_costs):
    """Usage-normalized measures for one harness; each metric reports its own n and the exclusions by reason."""
    ex = {'input': {}, 'output': {}, 'cost': {}}
    used = {'input': [], 'output': [], 'cost': []}
    lower = False
    subs = {'sessions': 0, 'matched': 0, 'excluded': 0}

    def skip(kinds, reason):
        for k in kinds:
            ex[k][reason] = ex[k].get(reason, 0) + 1
    for s, v in mine.items():
        subs['sessions'] += h == 'claude' and v['sub']
        if v['floor'] is None:
            skip(ex, 'no_floor')
            continue
        e, reason = _lookup(joined, h, s, v['sub'])
        if h == 'claude' and v['sub']:
            subs['matched' if e else 'excluded'] += 1
        if e is None:
            skip(ex, reason)
            continue
        fixed = v['floor'] * v['calls'] if e['input'] is None else min(v['floor'] * v['calls'], e['input'])
        if e['input'] is None:
            skip(('input', 'cost'), 'tokens_unknown')
        else:
            used['input'].append((fixed, e['input']))
        if e['output'] is None:
            skip(('output',), 'tokens_unknown')
        else:
            used['output'].append((fixed, e['output']))
            lower = lower or e['lower']
        if e['input'] is not None:
            if e['cost'] is None or rec_costs.get(s) is None:
                skip(('cost',), 'cost_unknown')
            else:
                used['cost'].append((rec_costs[s], e['cost']))
    inp, out, cost = used['input'], used['output'], used['cost']
    calls = [v['calls'] for v in mine.values() if v['floor'] is not None]
    floors = [v['floor'] for v in mine.values() if v['floor'] is not None]
    return {
        'floor_per_call': {'median': statistics.median(floors) if floors else None,
                           'p10': _pct(floors, 0.1) if floors else None, 'p90': _pct(floors, 0.9) if floors else None,
                           'n': len(floors)},
        'fixed_share_of_input': {'pct': _share(sum(f for f, _ in inp), sum(i for _, i in inp)),
                                 'median_session_pct': statistics.median(f / i * 100 for f, i in inp if i) if any(i for _, i in inp) else None,
                                 'n': len(inp)},
        'fixed_per_1k_output': {'value': _share(sum(f for f, _ in out), sum(o for _, o in out), 1000),
                                'output_lower_bound': lower, 'n': len(out)},
        'fixed_cost_share': {'pct': _cost_shares(cost), 'n': len(cost)},
        'calls_per_session': {'median': statistics.median(calls) if calls else None,
                              'p90': _pct(calls, 0.9) if calls else None, 'n': len(calls)},
        'excluded': ex, 'subagents': subs}


def summarize(db, since=None, until=None, harness=None, records=None, prices=None):
    """Per-harness floor distribution, estimated component tokens, residual, skill uses and recurring cost.

    `records` (History.records) supplies each session's dominant model; without it the cost stays None.
    """
    ensure(db)
    lo, hi = _moment(since), _moment(until)
    sessions = {}
    for h, s, sub, ts, floor, inp, write, read, calls, unidentified in db.execute(
            'SELECT harness, session, is_subagent, first_ts, floor_tokens, floor_input, floor_cache_write,'
            ' floor_cache_read, calls, unidentified_skill_reads FROM overhead_sessions ORDER BY harness, session'):
        when = _moment(ts)
        if harness not in (None, h) or (lo and (when is None or when < lo)) or (hi and (when is None or when >= hi)):
            continue
        sessions[(h, s)] = {'floor': floor, 'calls': calls, 'kinds': {}, 'skills': [], 'sub': bool(sub),
                          'unidentified': unidentified}
    for h, s, kind, chars in db.execute('SELECT harness, session, kind, chars FROM overhead_items'):
        if (h, s) in sessions:
            sessions[(h, s)]['kinds'][kind] = sessions[(h, s)]['kinds'].get(kind, 0) + chars
    for h, s, name, chars, source in db.execute('SELECT harness, session, name, chars, source FROM overhead_skill_uses'):
        if (h, s) in sessions:
            sessions[(h, s)]['skills'].append((name, chars, source))
    table, tariffs = None, {}
    if records is not None:
        from tokenatlas import pricing
        table = prices if prices is not None else pricing.load_prices()
        models, joined = _dominant(records), _joined(records, table)
        tariffs = _dominant_tariff(records, models)
    joined = joined if records is not None else {}
    result = {'chars_per_token': CHARS_PER_TOKEN, 'token_basis': 'estimate', 'harnesses': {}}
    for h in sorted({k[0] for k in sessions}):
        mine = {k[1]: v for k, v in sessions.items() if k[0] == h}
        floors = [v['floor'] for v in mine.values() if v['floor'] is not None]
        kinds, skills, residual = {}, {}, []
        for v in mine.values():
            for kind, chars in v['kinds'].items():
                kinds.setdefault(kind, []).append(chars)
            for name, chars, source in v['skills']:
                skills.setdefault((name, source), []).append(chars)
            if v['kinds'] and v['floor'] is not None:
                residual.append(v['floor'] - sum(v['kinds'].values()) / CHARS_PER_TOKEN)
        cost, priced, unpriced, rec_costs, assumptions = {}, 0, 0, {}, set()
        for s, v in mine.items():
            if v['floor'] is None:
                continue
            price, currency = None, None
            key = (h, s.split('/')[0])
            model = models.get(key) if table is not None else None
            tariff, mixed = tariffs.get(key, (None, False))
            if model and v['calls'] > 1:
                one = pricing.price_observation({'harness': h, 'provider': model[0], 'model': model[1], 'tariff': tariff,
                                                 'tokens': {'fresh_input': 0, 'cache_read': v['floor'], 'cache_write': 0,
                                                            'output': 0}}, table)
                price, currency = one['cost'], one['currency']
            elif model and v['calls'] <= 1:
                price = 0.0
            if price is None:
                unpriced += 1
            else:
                amount = price * max(v['calls'] - 1, 0)
                rec_costs[s] = {}
                if currency:  # a one-call session re-reads nothing and has no currency; it adds to no map
                    rec_costs[s] = {currency: amount}
                    _add_cost(cost, amount, currency)
                if mixed:
                    assumptions.add('mixed tariffs in session')
                priced += 1
        result['harnesses'][h] = {
            'sessions': len(mine), 'subagent_sessions': sum(v['sub'] for v in mine.values()),
            'floor': {'n': len(floors), 'median': statistics.median(floors) if floors else None,
                      'p10': _pct(floors, 0.1) if floors else None, 'p90': _pct(floors, 0.9) if floors else None},
            'components_available': bool(kinds),
            'components': {k: {'n': len(c), 'median_chars': statistics.median(c),
                               'estimated_tokens': statistics.median(c) / CHARS_PER_TOKEN, 'label': 'estimate'}
                           for k, c in sorted(kinds.items())},
            'residual': {'median_tokens': statistics.median(residual) if residual else None, 'label': RESIDUAL_LABEL},
            'unidentified_skill_reads': sum(v['unidentified'] for v in mine.values()),
            'skill_uses': [{'name': n, 'source': src, 'count': len(c), 'median_chars': statistics.median(c),
                            'estimated_tokens': statistics.median(c) / CHARS_PER_TOKEN, 'label': 'estimate'}
                           for (n, src), c in sorted(skills.items(), key=lambda i: (-len(i[1]), i[0]))],
            'relative': _relative(h, mine, joined, rec_costs),
            'recurring_cost': {'cost': cost if priced else None, 'assumptions': sorted(assumptions), 'sessions_priced': priced,
                               'sessions_unpriced': unpriced, 'label': COST_LABEL}}
    return result


def _n(value):
    return '-' if value is None else f'{value:,.0f}'


def _p(value):
    return '-' if value is None else f'{value:.1f}%'


def _by_currency(values, fmt):
    """`USD 12.34, EUR 1.20` for a {currency: value} map; '-' when empty."""
    return ', '.join(f'{c} {fmt(v)}' for c, v in sorted(values.items())) if values else '-'


def render(result):
    lines = [NOTE, '']
    rows = sorted(((h, r['relative']) for h, r in result['harnesses'].items()),
                  key=lambda i: (i[1]['fixed_share_of_input']['pct'] is None, i[1]['fixed_share_of_input']['pct'] or 0, i[0]))
    if rows:
        cols = ('harness', 'floor/call', 'fixed % of input', 'fixed per 1k output', 'fixed % of cost', 'calls/session', 'n')
        table = [cols]
        for h, x in rows:
            lb = '*' if x['fixed_per_1k_output']['output_lower_bound'] else ''
            table.append((h, _n(x['floor_per_call']['median']) + f" ({_n(x['floor_per_call']['p10'])}-{_n(x['floor_per_call']['p90'])})",
                          _p(x['fixed_share_of_input']['pct']) + f" (med {_p(x['fixed_share_of_input']['median_session_pct'])})",
                          _n(x['fixed_per_1k_output']['value']) + lb, (_p(next(iter(x['fixed_cost_share']['pct'].values()))) if len(x['fixed_cost_share']['pct']) == 1
                           else _by_currency(x['fixed_cost_share']['pct'], lambda v: f'{v:.1f}%')),
                          f"{_n(x['calls_per_session']['median'])} (p90 {_n(x['calls_per_session']['p90'])})",
                          f"{x['fixed_share_of_input']['n']}/{x['fixed_per_1k_output']['n']}/{x['fixed_cost_share']['n']}"))
        widths = [max(len(str(row[i])) for row in table) for i in range(len(cols))]
        lines += ['  '.join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
        lines.append('floor/call is median (p10-p90) tokens; n is sessions used for input/output/cost measures.')
        if any(x['fixed_per_1k_output']['output_lower_bound'] for _, x in rows):
            lines.append('* output is a lower bound (output_not_final observations), so fixed per 1k output is an upper bound.')
        for h, x in rows:
            gone = {k: v for k, v in x['excluded'].items() if v}
            if gone:
                lines.append(f"excluded {h}: " + '; '.join(f"{k}: " + ', '.join(f'{n} {r}' for r, n in sorted(v.items()))
                                                          for k, v in gone.items()))
            if x['subagents']['sessions']:
                lines.append(f"{h} subagents: {x['subagents']['matched']} matched to history by agent id, "
                             f"{x['subagents']['excluded']} excluded from the history join")
    lines += ['', f"Fixed context overhead; component tokens are estimates at {result['chars_per_token']} characters per token."]
    if not result['harnesses']:
        lines.append('No overhead data; run with --refresh.')
    for h, r in result['harnesses'].items():
        f = r['floor']
        lines += ['', f"{h}: {r['sessions']} sessions ({r['subagent_sessions']} subagent); floor tokens n={f['n']} "
                      f"median={_n(f['median'])} p10={_n(f['p10'])} p90={_n(f['p90'])}"]
        if not r['components_available']:
            lines.append('  components unavailable for this harness (floor only)')
        for kind, c in r['components'].items():
            lines.append(f"  {kind:<20} n={c['n']:<5} median {_n(c['median_chars']):>9} chars  ~{_n(c['estimated_tokens']):>8} tokens (estimate)")
        if r['residual']['median_tokens'] is not None:
            lines.append(f"  {r['residual']['label']}: median ~{_n(r['residual']['median_tokens'])} tokens (estimate)")
        rc = r['recurring_cost']
        lines.append(f"  recurring re-read cost (total, depends on usage): {'unknown' if rc['cost'] is None else _by_currency(rc['cost'], lambda v: format(v, ',.2f'))}"
                     f" over {rc['sessions_priced']} priced sessions ({rc['sessions_unpriced']} unpriced); {rc['label']}")
        for u in r['skill_uses']:
            lines.append(f"  skill {u['name']:<24} uses={u['count']:<4} median {_n(u['median_chars']):>8} chars  ~{_n(u['estimated_tokens']):>7} tokens ({u['source']}, estimate)")
        if r['unidentified_skill_reads']:
            lines.append(f"  unidentified skill reads: {r['unidentified_skill_reads']} (heuristic; not a concrete skill path or body)")
    return '\n'.join(lines)


def run(args):
    """`overhead` command: optional rescan of the default roots, then the report; returns the exit status."""
    from tokenatlas import why
    from tokenatlas.history import History
    if not args.refresh and not args.db.expanduser().is_file():
        raise ValueError('history database does not exist; run refresh or overhead --refresh first')
    roots = {h: why.harness_root(h)[0] for h in ('claude', 'codex', 'pi', 'opencode')}
    with History(args.db) as history:
        db = history.connection
        if args.refresh:
            for h in (args.harness,) if args.harness else HARNESSES:
                if Path(roots[h]).exists():
                    with progress.step(f'Scan {h}'):
                        db.execute('BEGIN')
                        save(db, h, scan(h, roots[h]))
        db.execute('BEGIN')
        with progress.step('Summarize overhead'):
            result = summarize(db, since=args.since, harness=args.harness, records=history.records())
        db.rollback()
    progress.finish()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return 0
