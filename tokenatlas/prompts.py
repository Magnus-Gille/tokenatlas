"""Top prompts: roll every request up to the user prompt (turn) that caused it, then rank the prompts."""
from bisect import bisect_right
from datetime import datetime

from tokenatlas import credits as credit_rates
from tokenatlas.pricing import price_observation
from tokenatlas.resume import resume_command

CLASSES = ('fresh_input', 'cache_write', 'cache_read', 'output', 'reasoning')
MAX_DEPTH = 8


def _t(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))


def ident(r):
    """Full observation identity: ids alone can repeat across providers and machines."""
    return (r.get('provider'), r['harness'], r.get('machine'), r['id'])


def _thread(r):
    """A subagent thread: a Claude subagent is (session, agent) inside its parent's session, other harnesses use a child session.
    Without a usable agent id the subagent's first source file stands in, so separate files stay separate threads."""
    if r['harness'] != 'claude':return (r['harness'], r['session'])
    agent = r.get('agent')
    if agent in (None, '', 'main', 'unknown') and r.get('sources'):agent = r['sources'][0]
    return (r['harness'], r['session'], agent)


def assign_prompts(records):
    """Parallel to records: (harness, root_session, turn_id, 'own' | 'rolled_up' | 'own_subagent') or None when unattributable.
    Positional, so duplicate ids never collide. A subagent thread binds to the parent turn running at its first observation,
    and all its observations follow it; only the orphan fallback (no parent turn found) is per observation."""
    at = [_t(r['ts']) for r in records]
    starts, parents = {}, {}  # (harness, session, turn) -> min ts; (harness, session) -> parent session of a subagent thread
    for r, ts in zip(records, at):
        if r['thread_kind'] == 'subagent':
            if r.get('parent_session'):parents.setdefault((r['harness'], r['session']), r['parent_session'])
        elif r.get('turn_id'):
            key = (r['harness'], r['session'], r['turn_id'])
            starts[key] = min(starts.get(key, ts), ts)
    turns = {}  # (harness, session) -> [(start, turn)] sorted by start
    for (harness, session, turn), start in starts.items():
        turns.setdefault((harness, session), []).append((start, turn))
    for found in turns.values():found.sort()
    first = {}  # thread -> earliest observation
    for r, ts in zip(records, at):
        if r['thread_kind'] == 'subagent':first[_thread(r)] = min(first.get(_thread(r), ts), ts)
    bound = {}  # thread -> (root session, turn) or None
    def bind(r):
        session, seen = r['session'], {r['session']}
        for _ in range(MAX_DEPTH):
            parent = parents.get((r['harness'], session))
            if parent is None or parent == session:break  # Claude subagents share their parent's session
            if parent in seen:return None  # cycle
            seen.add(parent);session = parent
        found = turns.get((r['harness'], session))
        n = bisect_right(found, (first[_thread(r)], chr(0x10FFFF))) - 1 if found else -1
        return (session, found[n][1]) if n >= 0 else None
    result = []
    for r in records:
        if r['thread_kind'] != 'subagent':
            result.append((r['harness'], r['session'], r['turn_id'], 'own') if r.get('turn_id') else None)
            continue
        t = _thread(r)
        if t not in bound:bound[t] = bind(r)
        if bound[t]:result.append((r['harness'], *bound[t], 'rolled_up'))
        else:result.append((r['harness'], r['session'], r['turn_id'], 'own_subagent') if r.get('turn_id') else None)
    return result


def _cost(r, table):
    """List-price cost in USD, or None: a non-USD price is never mixed into a USD sum."""
    priced = price_observation(r, table)
    if priced['cost'] is None:return None
    return priced['cost'] if priced.get('currency') == 'USD' or (priced.get('status') == 'free' and priced.get('currency') is None) else None


def top_prompts(records, table, k=5, by='cost', keep=None, credit_table=None, assigned=None):
    """Rank prompts by list-price cost (unpriced last) or total tokens. Assignment runs over all records; `keep`, a set of
    ident() values, then restricts which observations contribute (a filter window), so a subagent still rolls up to a parent turn outside it.
    `credits` is the turn's ChatGPT credit equivalent (credits.py), only when every request in it has a credit rate, else None; `credits_lower_bound`
    marks a sum over incomplete token counters."""
    if by not in ('cost', 'tokens'):raise ValueError("by must be 'cost' or 'tokens'")
    credit_table = credit_table or credit_rates.packaged()
    assigned = assign_prompts(records) if assigned is None else assigned
    groups, unattributed = {}, 0
    for r, found in zip(records, assigned):
        if keep is not None and ident(r) not in keep:continue
        if found:groups.setdefault(found[:3], []).append(r)
        else:unattributed += 1
    prompts = []
    for (harness, session, turn), rows in groups.items():
        own = [r for r in rows if r['thread_kind'] != 'subagent']
        subs = [r for r in rows if r['thread_kind'] == 'subagent']
        tokens = {c: sum((r['tokens'].get(c) or 0) for r in rows) for c in CLASSES}
        costs = [_cost(r, table) for r in rows]
        priced = [c for c in costs if c is not None]
        owed = [credit_rates.credit_observation(r, credit_table)['credits'] for r in rows]
        first, last = min((r['ts'] for r in rows), key=_t), max((r['ts'] for r in rows), key=_t)
        head = min(own or rows, key=lambda r: _t(r['ts']))
        stopped = [r['ts'] for r in rows if 'interrupted' in (r.get('flags') or ())]  # the user stopped the turn (logged, not inferred)
        prompts.append({
            'harness': harness, 'session': session, 'turn_id': turn, 'thread_kind': head['thread_kind'], 'machine': head.get('machine'),
            'project_id': head.get('project_id'), 'project_label': head.get('project_label'),
            'first_ts': first, 'last_ts': last, 'duration_s': (_t(last) - _t(first)).total_seconds(),
            'models': sorted({r['model'] for r in rows if r.get('model')}), 'requests': len(rows),
            'subagent_requests': len(subs), 'subagents': len({(r['session'], r.get('agent')) for r in subs}),
            'tokens': tokens, 'total_tokens': sum(tokens[c] for c in CLASSES if c != 'reasoning'),
            'cost': sum(priced) if priced else None, 'cost_complete': len(priced) == len(costs),
            'interrupted': bool(stopped), 'stopped_request_at': min(stopped, key=_t) if stopped else None,
            'credits': sum(owed) if None not in owed else None, 'credits_lower_bound': any(not r.get('complete', True) for r in rows),
            'resume': resume_command(harness, session, head.get('cwd'))})  # validated and quoted; Claude needs the directory
    if by == 'cost':
        prompts.sort(key=lambda p: (p['cost'] is None, -(p['cost'] or 0), -p['total_tokens']))
    else:
        prompts.sort(key=lambda p: -p['total_tokens'])
    return {'prompts': prompts[:k], 'total_prompts': len(prompts),
            'unattributed_observations': unattributed, 'by': by}
