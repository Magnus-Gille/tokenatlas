"""Session trees, outcome ratings and cost per passed result; local only, no network or LLM calls."""
from tokenatlas.cli_contract import NotFound
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from tokenatlas.history import _has_private_permissions, _owned_by_current_user

HARNESSES = ('claude', 'codex', 'pi', 'opencode')
HEADLESS = ('codex_exec', 'exec', 'sdk-cli', 'sdk-py', 'sdk-ts')
OUTCOMES = ('pass', 'partial', 'redo', 'wrong')
CLASSES = ('fresh_input', 'cache_write', 'cache_read', 'output', 'reasoning')
WINDOW_SLACK = timedelta(minutes=10)
_AGENT_FILE = re.compile(r'(?:^|/)agent-([^/]+)\.jsonl$')
_WORKFLOW = re.compile(r'subagents/workflows/(wf_[^/]+)/')


def _slashed(path):
    """Imported snapshots may carry Windows source paths; match both separators."""
    return path.replace('\\', '/')


def default_pricer(prices_path=None):
    """Return (price callable, table retrieved date) from tokenatlas.pricing; imported lazily."""
    from tokenatlas import pricing
    table = pricing.load_prices(prices_path)
    return (lambda obs: pricing.price_observation(obs, table)), table.get('retrieved_on') or 'unknown'


def _t(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(timezone.utc)


def _empty():
    return {'observations': 0, 'tokens': dict.fromkeys(CLASSES, 0), 'total': 0, 'cost': None, 'priced': 0,
            'cost_coverage': None, 'cost_status_counts': {}, 'lower_bound': False, 'first_ts': None,
            'last_ts': None, 'models': [], 'model_first': {}}


def _finish(a):
    a['models'] = sorted(a['model_first'], key=lambda m: _t(a['model_first'][m]))
    a['cost_coverage'] = a['priced'] / a['observations'] if a['observations'] else None
    return a


def _merge(a, b):
    """Add aggregate b into a."""
    a['observations'] += b['observations']; a['priced'] += b['priced']; a['total'] += b['total']
    for k in CLASSES:
        a['tokens'][k] += b['tokens'][k]
    for cur, value in (b['cost'] or {}).items():
        a['cost'] = a['cost'] or {}
        a['cost'][cur] = a['cost'].get(cur, 0.0) + value
    for k, v in b['cost_status_counts'].items():
        a['cost_status_counts'][k] = a['cost_status_counts'].get(k, 0) + v
    a['lower_bound'] = a['lower_bound'] or b['lower_bound']
    for k, pick in (('first_ts', min), ('last_ts', max)):
        if b[k]:
            a[k] = b[k] if not a[k] else pick(a[k], b[k], key=_t)
    for m, ts in b['model_first'].items():
        if m not in a['model_first'] or _t(ts) < _t(a['model_first'][m]):
            a['model_first'][m] = ts
    return _finish(a)


def _aggregate(rows, price):
    """Return (aggregate, {model: aggregate}) for observation rows."""
    total, by_model = _empty(), {}
    for row in rows:
        one = _empty()
        one['observations'] = 1
        for k in CLASSES:
            one['tokens'][k] = row['tokens'].get(k) or 0
        one['total'] = sum(one['tokens'][k] for k in CLASSES if k != 'reasoning')
        priced = price(row)
        status = priced.get('status') or 'unpriced'
        one['cost_status_counts'][status] = 1
        if priced.get('cost') is not None:
            one['cost'] = {priced.get('currency') or '?': float(priced['cost'])}; one['priced'] = 1
        one['lower_bound'] = (not row.get('complete', True)) or 'output_not_final' in (row.get('warnings') or ())
        one['first_ts'] = one['last_ts'] = row['ts']
        name = row['model'] or 'unknown'  # history normalizes an unrecorded model to None
        one['model_first'][name] = row['ts']
        _merge(total, _finish(one))
        _merge(by_model.setdefault(name, _empty()), one)
    return total, by_model


def _related(a, b):
    a, b = (a or '').rstrip('/'), (b or '').rstrip('/')
    return bool(a and b) and (a == b or a.startswith(b + '/') or b.startswith(a + '/'))


def _resolve(records, root):
    prefix, sep, rest = root.partition(':')
    if sep and prefix in HARNESSES:
        return prefix, rest
    found = sorted({(r['harness'], r['session']) for r in records if r['session'] == root})
    if not found:
        raise NotFound(f'no session {root!r} in history')
    if len(found) > 1:
        raise ValueError(f'session {root!r} is ambiguous; use one of: ' + ', '.join(f'{h}:{s}' for h, s in found))
    return found[0]


def thread_key(node):
    if node['kind'] == 'subagent':
        return f"claude:agent:{node['id']}"
    if node['kind'] == 'workflow':
        return f"claude:workflow:{node['id']}"
    return f"{node['harness']}:{node['id']}"


def _thread_of_key(key):
    harness, _, rest = key.partition(':')
    if harness == 'claude' and rest.startswith('agent:'):
        return {'harness': 'claude', 'agent_id': rest[6:]}
    if harness == 'claude' and rest.startswith('workflow:'):
        return {'harness': 'claude', 'workflow_run': rest[9:]}
    return {'harness': harness, 'session': rest}


def _thread_key_of(thread):
    if 'agent_id' in thread:
        return f"{thread.get('harness', 'claude')}:agent:{thread['agent_id']}"
    if 'workflow_run' in thread:
        return f"{thread.get('harness', 'claude')}:workflow:{thread['workflow_run']}"
    return f"{thread.get('harness')}:{thread.get('session')}"


def build_tree(records, root, *, infer=True, price=None):
    """Build the tree of one root session. Returns nodes, per-model totals, coordination and unassigned."""
    if price is None:
        price = default_pricer()[0]
    harness, session = _resolve(records, root)
    rows_of, subs, parents = {}, {}, {}
    for r in records:
        if r['harness'] == 'claude' and r.get('thread_kind') == 'subagent':
            subs.setdefault(r['session'], []).append(r)
        else:
            rows_of.setdefault((r['harness'], r['session']), []).append(r)
            if r.get('parent_session'):
                parents.setdefault((r['harness'], r['session']), set()).add(r['parent_session'])
    if (harness, session) not in rows_of and session not in subs:
        raise NotFound(f'no session {root!r} in history')
    visited = set()

    def restat(n):
        base = _merge(_empty(), n['own'])
        for c in n['children']:
            _merge(base, c)
        n.update(base)
        return n

    def node(kind, ident, link, label, rows, hh, children):
        own, by_model = _aggregate(rows, price)
        own['by_model'] = by_model
        return restat({'id': ident, 'harness': hh, 'kind': kind, 'link': link, 'label': label, 'own': own,
                       'children': children})

    rollup = restat

    def session_node(key, kind, link):
        visited.add(key)
        h, sid = key
        rows = rows_of.get(key, [])
        agent = next((r['agent'] for r in rows if r.get('agent') and r['agent'] != 'main'), None)
        label = f'{h}:{sid[:8]}' + (f' {agent}' if agent and kind == 'child' else '')
        children = []
        if h == 'claude':
            groups, workflows = {}, {}
            for r in subs.get(sid, []):
                found = next((m.group(1) for m in map(_AGENT_FILE.search, map(_slashed, r['sources'])) if m), None)
                groups.setdefault(found or r['agent'], []).append(r)
            for agent_id, agent_rows in groups.items():
                agent_type = next((x['agent'] for x in agent_rows if x.get('agent')), agent_id)
                name = agent_id if agent_type == agent_id else f'{agent_type} {agent_id[:8]}'
                match = next((m for x in agent_rows for m in map(_WORKFLOW.search, map(_slashed, x['sources'])) if m), None)
                leaf = node('subagent', agent_id, 'path', name, agent_rows, 'claude', [])
                if match:
                    workflows.setdefault(match.group(1), []).append(leaf)
                else:
                    children.append(leaf)
            for run, leaves in workflows.items():
                children.append(rollup(node('workflow', run, 'path', f'workflow {run}', [], 'claude', leaves)))
        for k in sorted(k for k in rows_of if k not in visited and sid in parents.get(k, ())):
            if k not in visited:
                children.append(session_node(k, 'child', 'explicit'))
        return rollup(node(kind, sid, link, label, rows, h, children))

    root_node = session_node((harness, session), 'main', 'root')
    unassigned = []
    if root_node['first_ts']:
        window = (_t(root_node['first_ts']), _t(root_node['last_ts']) + WINDOW_SLACK)
        rows = rows_of.get((harness, session), []) + subs.get(session, [])
        root_cwd = next((r['cwd'] for r in rows if r.get('cwd')), None)
        info = {}
        for key, rs in rows_of.items():
            if key in visited or parents.get(key):
                continue
            ts = [_t(r['ts']) for r in rs]
            info[key] = {'span': (min(ts), max(ts)), 'cwd': next((r['cwd'] for r in rs if r.get('cwd')), None),
                         'origin': next((r['origin'] for r in rs if r.get('origin')), None),
                         'subagent': any(r.get('thread_kind') == 'subagent' for r in rs), 'rows': rs}

        def fits(span, cwd, win, root_cwd_):
            return win[0] <= span[0] and span[1] <= win[1] and _related(cwd, root_cwd_)

        others = {}
        for key, i in info.items():
            if i['origin'] not in HEADLESS and not i['subagent']:
                others[key] = ((i['span'][0], i['span'][1] + WINDOW_SLACK), i['cwd'])
        for key in sorted(info):
            i = info[key]
            headless = i['origin'] in HEADLESS
            inside = _related(i['cwd'], root_cwd) and i['span'][1] >= window[0] and i['span'][0] <= window[1]
            if headless and fits(i['span'], i['cwd'], window, root_cwd):
                rivals = [f'{h}:{s}' for (h, s), (win, cwd) in others.items()
                          if (h, s) != key and fits(i['span'], i['cwd'], win, cwd)]
                if rivals and infer:
                    unassigned.append(_orphan(key, i, 'ambiguous: also fits ' + ', '.join(rivals)))
                elif infer:
                    root_node['children'].append(session_node(key, 'child', 'inferred'))
            elif i['subagent'] and inside:
                unassigned.append(_orphan(key, i, 'no parent_session; inside the root window and cwd'))
        restat(root_node)
    nodes = []

    def walk(n):
        nodes.append(n)
        for c in n['children']:
            walk(c)
    walk(root_node)
    models = {}
    for n in nodes:
        for m, a in n['own']['by_model'].items():
            _merge(models.setdefault(m, _empty()), a)
    grand = _empty()
    for a in models.values():
        _merge(grand, a)
    coordination = {k: v for k, v in root_node['own'].items() if k != 'by_model'}
    return {'root': root_node, 'root_key': thread_key(root_node), 'models': models, 'total': grand,
            'coordination': coordination, 'unassigned': unassigned, 'infer': infer}


def _orphan(key, i, reason):
    return {'harness': key[0], 'id': key[1], 'origin': i['origin'], 'reason': reason,
            'agent': next((r['agent'] for r in i['rows'] if r.get('agent')), None), 'observations': len(i['rows']),
            'first_ts': i['span'][0].isoformat(), 'last_ts': i['span'][1].isoformat()}


def iter_nodes(node):
    yield node
    for c in node['children']:
        yield from iter_nodes(c)


class Outcomes(list):
    malformed = 0
    skipped = 0


def load_outcomes(path, root):
    """Matching v0/v1 lines for a root session; malformed lines are counted, not fatal."""
    out = Outcomes()
    problem = _outcomes_problem(path)
    if problem:
        print(f'warning: outcomes file {problem}; reading it anyway', file=sys.stderr)
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return out
    wanted = {root, root.partition(':')[2]}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
            if not isinstance(item, dict) or item.get('v') not in (0, 1):
                raise ValueError('schema version')
            if not isinstance(item.get('root_session'), str) or not isinstance(item.get('unit'), str):
                raise ValueError('keys')
            if item.get('v') == 0 and item.get('outcome') is None:
                if item['root_session'] in wanted:
                    out.skipped += 1
                continue
            if item.get('outcome') not in OUTCOMES or not isinstance(item.get('threads'), list) \
                    or not all(isinstance(t, dict) for t in item['threads']):
                raise ValueError('outcome or threads')
        except ValueError:
            out.malformed += 1
            continue
        if item['root_session'] in wanted or item['root_session'].partition(':')[2] in wanted - {''}:
            out.append(item)
    return out


def efficiency(result, outcomes):
    """Cost per passed result per model. All rated units count; unit-level last rating wins."""
    index = {thread_key(n): n for n in iter_nodes(result['root']) if n is not result['root']}
    units, warnings, covered = {}, [], set()
    for o in outcomes:
        units[o['unit']] = o
    per_model = {}
    unit_out = []
    for name, o in units.items():
        matched, keys = [], []
        for t in o['threads']:
            key = _thread_key_of(t)
            if key == result['root_key']:
                warnings.append(f'{name}: coordination thread is not a unit; ignored')
            elif key not in index:
                warnings.append(f'{name}: unknown thread {key}')
            else:
                keys.append(key)
                for n in iter_nodes(index[key]):
                    if id(n) not in covered_ids(matched):
                        matched.append(n)
        unit_models = {}
        for n in matched:
            covered.add(id(n))
            for m, a in n['own']['by_model'].items():
                _merge(unit_models.setdefault(m, _empty()), a)
        for m, a in unit_models.items():
            slot = per_model.setdefault(m, {'agg': _empty(), 'units': {}, 'pass_units': 0})
            _merge(slot['agg'], a)
            slot['units'][o['outcome']] = slot['units'].get(o['outcome'], 0) + 1
            slot['pass_units'] += o['outcome'] == 'pass'
        unit_out.append({'unit': name, 'outcome': o['outcome'], 'note': o.get('note', ''), 'threads': keys,
                         'models': sorted(unit_models)})
    models = {}
    for m, s in sorted(per_model.items()):
        a, p = s['agg'], s['pass_units']
        models[m] = {'total': a['total'], 'observations': a['observations'], 'cost': a['cost'],
                     'cost_coverage': a['cost_coverage'], 'lower_bound': a['lower_bound'],
                     'units_by_outcome': s['units'], 'pass_units': p,
                     'tokens_per_pass': a['total'] / p if p else None,
                     'cost_per_pass': ({c: v / p for c, v in a['cost'].items()} if p and a['cost'] else None)}
    unrated = [k for k, n in index.items() if id(n) not in covered and not (n['kind'] == 'workflow' and not n['own']['observations'])]
    return {'units': unit_out, 'models': models, 'unrated': unrated, 'warnings': warnings,
            'malformed': getattr(outcomes, 'malformed', 0), 'skipped': getattr(outcomes, 'skipped', 0)}


def covered_ids(nodes):
    return {id(n) for n in nodes}


def _num(n):
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if n >= limit:
            return f'{n / limit:.1f}{suffix}'
    return str(int(n))


def _money(cost, coverage=1, lower=False):
    if not cost:
        return 'n/a'
    text = ' + '.join(f'${v:,.2f}' if c == 'USD' else f'{v:,.2f} {c}' for c, v in sorted(cost.items()))
    return ('≥' if lower or (coverage or 0) < 1 else '') + text


def _tokens(a):
    t = a['tokens']
    return (f"Input {_num(t['fresh_input'])} · Cache write {_num(t['cache_write'])} · Cache read {_num(t['cache_read'])}"
            f" · Output {_num(t['output'])} · Total {_num(a['total'])}")


def render(result, eff, retrieved):
    lines = [f'Costs are API-equivalent at list price (table retrieved {retrieved}), not what was paid.',
             'Node figures include children; ≥ marks lower bounds (unfinal output or unpriced observations).', '']

    def walk(n, depth):
        mark = '≥' if n['lower_bound'] else ''
        lines.append(f"{'  ' * depth}{n['label']} [{n['link']}] {', '.join(n['models']) or '-'}  "
                     f"{mark}{_tokens(n)}  {_money(n['cost'], n['cost_coverage'], n['lower_bound'])}")
        for c in n['children']:
            walk(c, depth + 1)
    walk(result['root'], 0)
    lines += ['', 'Per model:']
    for m, a in sorted(result['models'].items()):
        cov = f"{a['cost_coverage']:.0%}" if a['cost_coverage'] is not None else 'n/a'
        lines.append(f"  {m}: {a['observations']} obs  {_tokens(a)}  {_money(a['cost'], a['cost_coverage'], a['lower_bound'])} (coverage {cov})")
    g = result['total']
    lines.append(f"  total: {g['observations']} obs  {_tokens(g)}  {_money(g['cost'], g['cost_coverage'], g['lower_bound'])}")
    c = result['coordination']
    lines += ['', f"Coordination (root main thread): {_tokens(c)}  {_money(c['cost'], c['cost_coverage'], c['lower_bound'])}"]
    if result['unassigned']:
        lines += ['', 'Unassigned (not linked, never guessed):']
        lines += [f"  {u['harness']}:{u['id']} ({u['origin']}, {u['agent'] or '-'}, {u['first_ts']}..{u['last_ts']}): {u['reason']}"
                  for u in result['unassigned']]
    if eff:
        lines += ['', 'Efficiency (all rated units ÷ passed units; n = units per outcome):']
        for u in eff['units']:
            lines.append(f"  unit {u['unit']}: {u['outcome']} ({', '.join(u['models']) or '-'})")
        for m, e in eff['models'].items():
            n = ' '.join(f'{k}={v}' for k, v in sorted(e['units_by_outcome'].items()))
            per = 'n/a' if e['tokens_per_pass'] is None else f"{_num(e['tokens_per_pass'])} tokens, {_money(e['cost_per_pass'], e['cost_coverage'], e['lower_bound'])}"
            lines.append(f"  {m}: per pass {per}  (n: {n})")
        lines += [f'  unrated: {k}' for k in eff['unrated']] or []
        lines += [f'  warning: {w}' for w in eff['warnings']]
        if eff['malformed']:
            lines.append(f"  {eff['malformed']} malformed outcome line(s) ignored")
    return '\n'.join(lines)


def _descriptor_problem(info):
    """Why an opened outcomes file is not private (not a regular file, foreign owner, loose mode); else None."""
    if not stat.S_ISREG(info.st_mode) or not _owned_by_current_user(info):
        return 'must be a regular file owned by this user'
    if not _has_private_permissions(info):
        return 'permissions must be 0600'
    return None


def _outcomes_problem(path):
    """Why an outcomes file is not private (symlink, not a regular file, foreign owner, loose mode); else None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return 'must not be a symlink'
    return _descriptor_problem(info)


def append_outcome(path, item):
    """Atomic single-write append; a new file is created 0600, an existing one must already be private."""
    data = (json.dumps(item, sort_keys=True) + '\n').encode()
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Validate the descriptor we actually opened, not the path, so a swap cannot slip past the check.
        try:
            fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0))
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ValueError('outcomes file must not be a symlink') from None
            raise
        try:
            problem = _descriptor_problem(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        if problem:
            os.close(fd)
            raise ValueError(f'outcomes file {problem}') from None
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
