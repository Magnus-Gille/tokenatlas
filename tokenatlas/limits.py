"""Limit hits: the turn that hit the 5-hour or weekly limit, and what tokenatlas saw filling that window.

A hit is a fact the logs state (a rejected Claude request, or a Codex observation whose rate_limits name a reached limit). What filled the
window is only what these logs contain: usage on claude.ai, ChatGPT or other machines counts toward the same limit but is not seen. Costs are
list prices in USD; a request without a complete USD price is counted as unpriced, never guessed."""
from datetime import datetime, timedelta, timezone

from tokenatlas import prompts, why

PROVIDER = {'claude': 'anthropic', 'codex': 'openai'}
TOP = 3
RETRY_GAP = timedelta(minutes=10)  # rejections without a reset time belong to one hit only within this gap
# The only limit types a shared report or the cost facts may name; any other string is neutral ("other").
PUBLIC_REACHED = frozenset(('five_hour', 'seven_day', 'rate_limit_reached', 'workspace_owner_credits_depleted', 'workspace_member_credits_depleted',
                            'workspace_owner_usage_limit_reached', 'workspace_member_usage_limit_reached', 'window_full'))


def public_reached(reached):
    return reached if reached in PUBLIC_REACHED or reached is None else 'other'


def _iso(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    except ValueError:
        return None


def _reset(value):
    """A reset time as canonical UTC ISO text, or None: stored or imported text is never exported as it is."""
    at = _iso(value)
    try:
        return at.astimezone(timezone.utc).isoformat() if at is not None and at.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _windows(quota):
    """The quota's windows, re-validated at the point of use (stored or imported data may predate the validators): a dict with an integer duration of
    1..527040 minutes and a finite used_percent; anything else is skipped."""
    out = []
    for w in (quota or {}).get('windows') or []:
        if not isinstance(w, dict):
            continue
        minutes, used = w.get('minutes'), w.get('used_percent')
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= why.MAX_WINDOW_MINUTES:
            continue
        if isinstance(used, bool) or not isinstance(used, (int, float)) or used != used or used in (float('inf'), float('-inf')):
            continue
        out.append(dict(w, resets_at=_reset(w.get('resets_at'))))
    return out


def _window(quota):
    """The window that was hit: the one with the highest used_percent (the first on a tie), or None."""
    windows = _windows(quota)
    return max(windows, key=lambda w: w.get('used_percent') or 0) if windows else None


def _scope(row):
    """The hit's origin row metadata, for scoped reports only (never exported): project, session, turn, model, effort, agent and provider; and `origin`,
    the series the hit belongs to (limit id, plan type, root session), which quota_share matches a hit to its window instance by."""
    quota = row.get('quota') or {}
    return dict(scope={k: row.get(k) for k in ('project_id', 'project_label', 'session', 'turn_id', 'model', 'effort', 'agent', 'provider')},
                origin=dict(limit_id=quota.get('limit_id'), plan_type=quota.get('plan_type'), session=row.get('parent_session') or row.get('session')))


def _hit_window(quota):
    """The window a Codex hit names: the only window at 100 % or more, else None (an unknown type with no single full window names none)."""
    full = [w for w in _windows(quota) if w['used_percent'] >= 100]
    return full[0] if len(full) == 1 else None


def label(minutes, reached=None):
    """('five_hour' | 'weekly' | raw text, minutes): a window is named by its length, never by its slot."""
    return 'five_hour' if minutes == 300 else 'weekly' if minutes == 10080 else (reached or None)


def limit_hits(records, events, table, assigned=None):
    """Hits, oldest first: {harness, at, reached, window_minutes, resets_at, retries, turn, window}. `turn` is (harness, session, turn_id) of
    the request running at the hit, or None. `window` (None when the window length is unknown) is {start, end, requests, priced_requests,
    unpriced_requests, cost, top: [{turn, requests, cost, share}]}: the same provider's list-price cost in [resets_at - minutes, at] and
    the three costliest turns in it as a share of that cost (unattributed requests count in the cost, not in the ranking)."""
    records, events = sorted(records, key=lambda r: r['ts']), sorted(events, key=lambda r: r['ts'])
    found = []
    claude, recent = {}, {}  # by reset time; and, for events without one, the latest hit per (harness, type) with its last event time
    for event in events:
        quota = event.get('quota') or {}
        if quota.get('status') != 'rejected':
            continue  # Codex quota events feed the episode detector below
        window = _window(quota)
        resets = (window.get('resets_at') if window else None) or _reset(quota.get('resets_at'))
        at = prompts._t(event['ts'])
        key = (event['harness'], quota.get('reached'), resets)
        # Retries are one hit: the same reset time, or adjacent (within RETRY_GAP of the episode's previous event) and not contradicting a known
        # reset time, whether or not this event carries one.
        hit = claude.get(key) if resets is not None else None
        if hit is None:
            known = recent.get(key[:2])
            known_reset = known and _iso(known[0]['resets_at'])
            if known and at - known[1] <= RETRY_GAP and (resets is None or known[0]['resets_at'] is None or known[0]['resets_at'] == resets) \
                    and not (resets is None and known_reset and at >= known_reset):  # a resetless retry only belongs to a hit before its known reset
                hit = known[0]
        if hit is not None:
            hit['retries'] += 1
            if hit['resets_at'] is None and resets is not None:  # keep the known reset
                hit['resets_at'] = resets
                claude[key] = hit
            if hit['window_minutes'] is None and window:
                hit['window_minutes'] = window.get('minutes')
            recent[key[:2]] = (hit, at)
            continue
        hit = dict(harness=event['harness'], at=event['ts'], reached=quota.get('reached'), window_minutes=window and window.get('minutes'),
                   resets_at=resets, retries=1, _row=event, **_scope(event))
        if resets is not None:
            claude[key] = hit
        recent[key[:2]] = (hit, at)
        found.append(hit)
    # Codex. Time-window series own all episode state: a series is (harness, limit id, plan, window minutes) and its episode is one hit, at its
    # earliest observation. It opens when the window is full, closes on a reading below 100 % from a session that reported it full, on a gap
    # longer than the window, or on a new reset instance. A non-credit named reached type is only a label: with full windows it labels their
    # episodes; with none it labels an open window episode of the scope, else opens a provisional "unknown window" episode that migrates into
    # the first window series that becomes full (keeping its time and turn). Credit exhaustion is an independent series with no window.
    # Observations with missing or empty windows are neutral for window series.
    ser = {}   # window and credits series -> open episode {hit, sessions that reported it, last full/on time, latest reset time}
    prov = {}  # scope -> the provisional unknown-window episode {hit, reached, sessions, last}
    done = {}  # window series -> reset times of the window instances already reported (a reset time names one window instance)
    series = sorted([*records, *(e for e in events if (e.get('quota') or {}).get('status') == 'event')], key=lambda r: r['ts'])  # usage quotas and quota events, in time order
    for record in series:
        quota = record.get('quota') or {}
        if quota.get('status') == 'rejected' or not quota:
            continue  # a record without quota says nothing about a limit and leaves every series alone
        reached, session, at = quota.get('reached'), record['session'], prompts._t(record['ts'])
        scope = (record['harness'], quota.get('limit_id'), quota.get('plan_type'))  # never across harnesses, limits or plans
        windows = _windows(quota)
        credits = bool(reached) and 'credits' in reached
        full, opened, full_w = [], set(), {}  # window series full in this observation; those it opened; their windows
        for key, ep in list(ser.items()):  # elapsed-time expiry runs for every open window episode of the scope, whatever this snapshot carries
            if ep and key[:3] == scope and key[3] == 'window' and at - ep['last'] > timedelta(minutes=key[4]):
                ser[key] = None
        for w in windows:
            m, key, reset = w['minutes'], scope + ('window', w['minutes']), _iso(w['resets_at'])
            ep = ser.get(key)
            # Closes on a gap longer than the window, or on a new reset instance (the reset moved past the old one by more than half the window).
            if ep and (at - ep['last'] > timedelta(minutes=m)
                       or (reset and ep['reset'] and at >= ep['reset'] and reset - ep['reset'] > timedelta(minutes=m / 2))):
                ep = ser[key] = None
            if w['used_percent'] >= 100:
                full.append(key)
                full_w[key] = w
                if ep:
                    ep['sessions'].add(session)
                    ep['last'], ep['reset'] = at, reset or ep['reset']
                    continue
                hit = None
                p = prov.get(scope)
                if p and at - p['last'] > timedelta(minutes=m):
                    prov.pop(scope)  # too old to be this window's evidence: it stays an unknown-window hit and a new window episode opens
                    p = None
                if p and not opened and p['hit'] is not None:
                    # The provisional episode migrates into the first window that becomes full: its time and turn stay, it learns the window.
                    hit, p['hit']['window_minutes'], p['hit']['resets_at'] = p['hit'], m, w['resets_at']
                    ep_sessions = p['sessions'] | {session}
                    prov.pop(scope)
                    if reset is not None:
                        done.setdefault(key, []).append(reset)
                    ser[key] = dict(hit=hit, sessions=ep_sessions, last=at, reset=reset)
                    opened.add(key)
                    continue
                # Readings of one window instance can flap around 100 % (several sessions, accounts or rolling estimates): the same reset time is the same hit.
                if not (reset is not None and any(abs(reset - r) <= RETRY_GAP for r in done.get(key, ()))):
                    hit = dict(harness=record['harness'], at=record['ts'], reached='window_full', window_minutes=m, resets_at=w['resets_at'],
                               retries=1, rolling=True, _row=record, **_scope(record))
                    found.append(hit)
                    if reset is not None:
                        done.setdefault(key, []).append(reset)
                ser[key] = dict(hit=hit, sessions={session}, last=at, reset=reset)
                opened.add(key)
            elif ep and session in ep['sessions']:
                ser[key] = None  # recovery: a reading below 100 % from a session that reported the window full
        # Recovery of the other series: a credits series (or the provisional episode) ends when a participating session reports another type, or none;
        # the provisional episode also ends on a reading with windows where none is full.
        for key, ep in list(ser.items()):
            if ep and key[:3] == scope and key[3] == 'credits' and key[4] != reached and session in ep['sessions']:
                ser[key] = None
        p = prov.get(scope)
        if p and session in p['sessions'] and (p['reached'] != reached or (windows and not full)):
            prov.pop(scope)
            p = None
        if credits:
            key = scope + ('credits', reached)
            ep = ser.get(key)
            if ep:
                ep['sessions'].add(session)
                ep['last'] = at
            else:
                hit = dict(harness=record['harness'], at=record['ts'], reached=reached, window_minutes=None, resets_at=None, retries=1, rolling=True,
                           _row=record, **_scope(record))
                found.append(hit)
                ser[key] = dict(hit=hit, sessions={session}, last=at, reset=None)
        elif reached:
            open_windows = [k for k, e in ser.items() if e and k[:3] == scope and k[3] == 'window']
            if full:
                # The type labels the windows it was reported with. A window that fills later, while the type already labels another window's open
                # episode, is its own crossing and keeps the neutral label.
                labelled = any(ser[k]['hit'] and ser[k]['hit']['reached'] == reached and k not in opened for k in open_windows)
                for key in full:
                    hit = ser[key]['hit']
                    if hit is not None and hit['reached'] == 'window_full' and not (labelled and key in opened):
                        hit['reached'] = reached
            elif open_windows:
                for key in open_windows:  # no full window in this observation: label an open episode, create no state
                    hit = ser[key]['hit']
                    if hit is not None and hit['reached'] == 'window_full':
                        hit['reached'] = reached
            elif p:
                p['sessions'].add(session)
                p['last'] = at
            else:
                hit = dict(harness=record['harness'], at=record['ts'], reached=reached, window_minutes=None, resets_at=None, retries=1, rolling=True,
                           _row=record, **_scope(record))
                found.append(hit)
                prov[scope] = dict(hit=hit, reached=reached, sessions={session}, last=at)
    if not found:
        return []  # the usual case: skip the turn assignment over the whole history
    found.sort(key=lambda h: h['at'])
    # One assignment, over usage only (as top_prompts and the report do); a rejection carries its own turn and never feeds the assignment.
    assigned = {id(r): a for r, a in zip(records, prompts.assign_prompts(records) if assigned is None else assigned)}
    threads = {}  # subagent thread -> [(time, assigned turn)] of its usage, for a rejection in a subagent file (it carries no usable turn of its own)
    for r in records:
        if r['thread_kind'] == 'subagent' and assigned[id(r)]:
            threads.setdefault(prompts._thread(r), []).append((prompts._t(r['ts']), tuple(assigned[id(r)][:3])))
    for found_turns in threads.values():
        found_turns.sort(key=lambda x: x[0])
    pending = []
    for hit in found:
        row = hit.pop('_row')
        pending.append((hit, row))
        turn = assigned.get(id(row))
        hit['turn'] = tuple(turn[:3]) if turn else (row['harness'], row['session'], row['turn_id']) if row.get('turn_id') else None
        bound = threads.get(prompts._thread(row)) if not turn and row.get('thread_kind') == 'subagent' else None
        if bound:  # the latest usage in the thread at or before the rejection, else the earliest after it
            at = prompts._t(row['ts'])
            before = [x for x in bound if x[0] <= at]
            hit['turn'] = (before[-1] if before else bound[0])[1]
            hit['_bound'] = True
        hit['window'] = _fill(hit, records, assigned, table)
    # A subagent whose first request was rejected has no usage to bind it: resolve it with the same assignment over usage plus those rejections,
    # in a side computation, so the usage assignments above (and the cards) are untouched.
    loose = [(hit, row) for hit, row in pending if row.get('thread_kind') == 'subagent' and not hit.get('_bound')]
    if loose:
        side = prompts.assign_prompts([*records, *(row for _, row in loose)])[len(records):]
        for (hit, _), turn in zip(loose, side):
            if turn and turn[3] == 'rolled_up':
                hit['turn'] = tuple(turn[:3])
    for hit in found:
        hit.pop('_bound', None)
    return found


def _fill(hit, records, assigned, table):
    """What was seen in the window before a hit. Claude's 5-hour limit is a session block and its weekly limit resets at a fixed time, so the
    window is [resets_at - minutes, hit]. Codex windows are rolling ("rolling window duration" in its protocol) and the reset time moves,
    so the window is [hit - minutes, hit]."""
    resets, minutes, at = _iso(hit['resets_at']), hit['window_minutes'], _iso(hit['at'])
    rolling = hit.get('rolling')
    if (resets is None and not rolling) or not minutes or at is None:
        return None
    start = at - timedelta(minutes=minutes) if rolling else resets - timedelta(minutes=minutes)
    aliases = table.get('provider_aliases') or {}
    canon = lambda p: aliases.get(p, p)
    provider = canon(PROVIDER.get(hit['harness']))
    turns, requests, priced, total, lower = {}, 0, 0, 0.0, False  # lower: some request is unpriced or incomplete, so totals are lower bounds
    for record in records:
        if record['harness'] != hit['harness'] or canon(record.get('provider')) != provider or record.get('id_synthetic') or not start <= prompts._t(record['ts']) <= at:
            continue  # an ambiguous identity is in no total, as in the cost facts
        requests += 1
        cost = prompts._cost(record, table)
        short = cost is None or not record.get('complete', True)
        lower = lower or short
        found = assigned.get(id(record))
        entry = turns.setdefault(tuple(found[:3]), [0.0, 0, 0, record['ts'], False]) if found else [0.0, 0, 0, record['ts'], False]
        entry[1] += 1
        entry[4] = entry[4] or short
        entry[3] = min(entry[3], record['ts'], key=prompts._t)
        if cost is not None:
            priced += 1
            total += cost
            entry[0] += cost
            entry[2] += 1
    ranked = sorted(((k, v) for k, v in turns.items() if v[2]), key=lambda kv: (-kv[1][0], kv[0]))[:TOP]
    return dict(start=start.isoformat(), end=at.isoformat(), requests=requests, priced_requests=priced, unpriced_requests=requests - priced,
                cost=total if priced else None, lower_bound=lower,
                top=[dict(turn=k, requests=v[1], cost=v[0], share=v[0] / total if total > 0 else None, first_ts=v[3], lower_bound=v[4]) for k, v in ranked])


def hit_turns(hits):
    """{(harness, session, turn_id): hit}: the first hit of each turn, for marking turn cards."""
    out = {}
    for hit in hits:
        if hit['turn'] and hit['turn'] not in out:
            out[hit['turn']] = hit
    return out


def limit_hit(hit):
    return dict(reached=hit['reached'], window_minutes=hit['window_minutes'], at=hit['at'])


def mark_turns(items, hits):
    """Add `limit_hit` to each ranked turn (dict with harness, session, turn_id) that hit a limit; others are left untouched."""
    turns = hit_turns(hits)
    for item in items:
        hit = turns.get((item['harness'], item['session'], item['turn_id']))
        if hit:
            item['limit_hit'] = limit_hit(hit)
    return items


def badge(hit):
    """English words for a turn card: names the window by its length."""
    name = label(hit['window_minutes'], hit['reached'])
    return {'five_hour': 'Hit the 5-hour limit', 'weekly': 'Hit the weekly limit'}.get(name) or (f'Hit a limit ({name})' if name and name != 'window_full' else 'Hit a limit')


def scope_hits(hits, records, harness=None, start=None, end=None, project=None, session=None, turn=None, model=None, effort=None, provider=None, agent=None, universe=None, assigned=None):
    """Hits that belong to a filtered report: the hit's harness and time must match the harness and start/end filters. With a project, session,
    turn, model, effort, provider or agent filter a hit is kept when its turn is among the filtered `records`' turns or when the hit's own row
    (a rejection has no usage record) matches every given filter; `universe` is the whole history's usage, the basis of the hits' turns. The hits themselves are computed over the whole history first."""
    given = {'project_id': project, 'turn_id': turn, 'model': model, 'effort': effort, 'provider': provider, 'agent': agent}
    given = {k: v for k, v in given.items() if v is not None}
    prefix = None
    if isinstance(session, str):
        head, separator, rest = session.partition(':')
        if separator and head in ('claude', 'codex', 'pi', 'opencode'):
            prefix, session = head, rest
    if prefix is not None and harness is not None and prefix != harness:
        return []  # as History.records: a session prefix that contradicts the harness filter selects nothing
    harness = harness or prefix
    if session is not None:
        given['session'] = session
    turns = None
    if given:
        # Hits carry rolled-up turn identities, so map the filtered records through the same whole-history assignment.
        everything = universe if universe is not None else records
        found = {prompts.ident(r): a for r, a in zip(everything, prompts.assign_prompts(everything) if assigned is None else assigned)}
        turns = {tuple(found[prompts.ident(r)][:3]) if found.get(prompts.ident(r)) else (r['harness'], r['session'], r.get('turn_id')) for r in records}
    out = []
    for hit in hits:
        at = prompts._t(hit['at'])
        if not ((harness is None or hit['harness'] == harness) and (start is None or at >= start) and (end is None or at < end)):
            continue
        if turns is not None and hit['turn'] not in turns and any((hit.get('scope') or {}).get(k) != v for k, v in given.items()):
            continue
        out.append(hit)
    return out
