"""A turn's share of the subscription limit, from the quota snapshots an agent writes with each request (docs/quota.md).

A snapshot is the account-wide used percentage of one window, as reported with a request (after it). A window instance is keyed by
(harness, account, minutes, resets_at). A turn's share is **observed** when nothing else ran in the same window meanwhile: the percentage at
the turn's last request minus the latest percentage seen before its first request. When other turns ran in the window at the same time, the
movement tokenatlas observed is spread over those turns by list-price cost and labeled an **estimate**; a turn's cost is never converted with
a fixed dollars-per-percent rate. Usage tokenatlas does not see (other machines, chat, cloud tasks) moves the percentage too.
The counter is whole-percent: a share is shown without decimals, and '< 1%' when it did not move."""
import functools
import gc
import math
import json
import os
from collections import OrderedDict
from bisect import bisect_left, bisect_right
from datetime import timedelta
from itertools import islice
from operator import itemgetter

from tokenatlas import limits, prompts

WEEK, FIVE_HOURS = 10080, 300
# Parallel sessions report their counter out of order (measured on real Codex data: thousands of small decreases inside one window), so a
# lower reading is a stale one, not a reset, unless it falls by more than RESET_DROP points to under RESET_RATIO of the highest value seen.
RESET_DROP, RESET_RATIO = 5, 0.5
COUNTER_GAP = 10
NARROW = 5  # a range of at most this many points also shows a point estimate
CONFLICT_EACH = 2
STRAGGLE, STRAGGLE_POINTS = timedelta(minutes=2), 2
CONFLICT_WINDOW = timedelta(minutes=10)


def _nogc(fn):
    """Run without the cyclic garbage collector: these passes allocate a few hundred thousand small dicts and keep them all, which makes
    every generation-2 collection a full scan for nothing (it roughly halves the time on a 400k-record history)."""
    @functools.wraps(fn)
    def run(*args, **kw):
        was = gc.isenabled()
        gc.disable()
        try:
            return fn(*args, **kw)
        finally:
            if was:
                gc.enable()
    return run


def _window_hit(reached, full, w):
    """True: this window is the one a reached limit names (the only one at 100%); None: it is at 100% with another window, so which one was
    reached is unknown; False: not this window (or the type is depleted credits, or no window is full)."""
    if not _hit(reached) or not any(w is f for f in full):
        return False
    return True if len(full) == 1 else None


def _hit(reached):
    return bool(reached) and 'credits' not in reached  # depleted credits are not a full time window


class Snapshots(list):
    """The snapshots, plus what turn_shares needs to see everyone who may have moved the counter: `requests` = {participant: [first, last,
    [(time, record index), ...]]} over every request of the harnesses that carry a quota (with a snapshot or not; a participant is a turn, or
    an unattributed session as (harness, session, None)), and `bearing` = the participants that have a snapshot."""
    requests = None
    bearing = frozenset()
    events = ()


def _when(value):
    try:
        return prompts._t(value) if isinstance(value, str) else None
    except ValueError:
        return None


ATTACH = timedelta(minutes=5)  # a Claude reading is placed at its session's latest request only when that was this recent; an idle one is not
CLAUDE_WINDOWS = (('five_hour', FIVE_HOURS), ('seven_day', WEEK))


def read_claude(path):
    """(rows, malformed) from a claude-quota.jsonl (written by `tokenatlas statusline --record-quota`): rows = [(t, ts, session, [(minutes,
    used_percent, resets_at ISO)])] (a missing session becomes a unique string per line) in file order, only lines with a valid time and at least one valid window; `malformed` counts the lines
    that are not (a torn or foreign line is skipped, never an error). A missing or unreadable file is ([], 0)."""
    rows, bad = [], 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for n, text in enumerate(f):
                if not text.strip():
                    continue
                try:
                    row = json.loads(text)
                    t, session = _when(row['ts']), row.get('session')
                    wins = []
                    for key, minutes in CLAUDE_WINDOWS:
                        w = row.get(key)
                        if not isinstance(w, dict):
                            continue
                        used, due = w.get('used_percent'), limits._reset(w.get('resets_at'))
                        if isinstance(used, bool) or not isinstance(used, (int, float)) or used != used or used in (float('inf'), float('-inf')) or due is None:
                            continue
                        wins.append((minutes, used, due))
                    if t is None or t.tzinfo is None or not wins or not (session is None or isinstance(session, str)):
                        raise ValueError('not a snapshot')
                except (ValueError, KeyError, TypeError, AttributeError):
                    bad += 1
                    continue
                rows.append((t, row['ts'], session or f'\x00{n}', wins))  # a reading without a session is its own participant: never matched, never compared with a real id
    except OSError:
        return [], 0
    return rows, bad


def claude_token(path):
    """A cheap change token of the snapshot file (size, mtime), None when there is none: the report cache keys on it."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def claude_snapshots(path, records, assigned, rows=None):
    """The Claude statusline snapshots as the snapshot dicts snapshots_from_records makes for Codex: harness and account 'claude', no plan,
    windows 300 (five_hour) and 10080 (seven_day). A reading is the counter after its session's latest Claude request at or before it: it keeps
    its own time (`t`, which is what the allocation intervals use) and takes the turn and record index of that request, whose time is `at`; with
    no such request within ATTACH, or when that request is outside the window instance the reading reports (half-open: resets_at minus the window length, up to but not including resets_at), it belongs to
    no turn for that window: turn None, an unattributed participant (harness, session, None). `assigned` is parallel to `records` or a dict by index, as
    snapshots_from_records takes it. A line that is no snapshot is skipped (see read_claude). Returns the list."""
    rows = read_claude(path)[0] if rows is None else rows
    if not rows:
        return []
    by = {}  # session -> its Claude requests (time, index), subagents included
    for i, r in enumerate(records):
        if r['harness'] == 'claude':
            t = _when(r['ts'])
            if t is not None:
                by.setdefault(r.get('parent_session') or r['session'], []).append((t, i))  # a subagent's requests belong to its parent's session
    index = {}
    for session, found in by.items():
        found.sort(key=lambda x: x[0])
        index[session] = ([t for t, _ in found], [i for _, i in found])
    out = []
    for t, ts, session, wins in rows:
        i = None
        if session in index:
            times, idx = index[session]
            n = bisect_right(times, t)
            if n and t - times[n - 1] <= ATTACH:
                i = idx[n - 1]
        at = None if i is None else _when(records[i]['ts'])
        root = ('claude', session if i is None else records[i].get('parent_session') or records[i]['session'])
        for minutes, used, due in wins:
            j = i
            if j is not None and not prompts._t(due) - timedelta(minutes=minutes) <= at < prompts._t(due):
                j = None  # the request is outside this window instance (before it began, or after it ended: a stale reading): it cannot have moved it
            a = (assigned[j] if isinstance(assigned, list) else assigned.get(j)) if j is not None else None
            who = tuple(a[:3]) if a else ('claude', session if j is None else records[j]['session'], None)
            out.append(dict(ts=ts, t=t, at=None if j is None else at, harness='claude', account='claude', plan_type=None, window=(minutes, due), used_percent=used, reached=None,
                            turn=who if who[2] is not None else None, who=who, i=j, resets=due, root=root, hit=False, due=prompts._t(due),
                            key=('claude', 'claude', minutes, None, None, 0, 0)))
    return out


@_nogc
def snapshots_from_records(records, assigned=None, events=(), claude=None):
    """One snapshot per window per observation that carries a quota: {ts, t, harness, account (limit_id or harness), plan_type, window:
    (minutes, resets_at), used_percent, reached, turn: (harness, session, turn_id) or None, who (turn, or (harness, session, None) for usage
    that no turn owns), hit, i (index in `records`), key}. Rejected limit events and windows that fail validation are skipped. `assigned`
    is parallel to `records`, as prompts.assign_prompts gives it (computed here when not given). `events` are quota-only observations (zero-token
    records with quota status 'event', see History.limit_events): they are placed in their window instances and returned as `.events`, for a
    window's peak, hit and reading count only; they are never requests, costs or allocation. `claude` is the path of a claude-quota.jsonl
    (claude_snapshots): its readings are added, and every Claude request becomes a participant. The result carries `requests` and `bearing`."""
    carriers, harnesses, reading = [], set(), {}  # reading: record index -> its valid windows with their parsed reset times
    for i, r in enumerate(records):
        quota = r.get('quota')
        if quota and quota.get('status') != 'rejected':
            valid = [(w, d) for w in limits._windows(quota) if (d := _when(w.get('resets_at'))) is not None]
            if valid:
                carriers.append(i)
                harnesses.add(r['harness'])
                reading[i] = valid
    rows = read_claude(claude)[0] if claude else []
    if rows:
        harnesses.add('claude')
    out = Snapshots()
    quiet = []  # quota-only events as snapshots
    for e in events:
        quota = e.get('quota') or {}
        t = _when(e.get('ts'))
        if quota.get('status') != 'event' or t is None:
            continue
        valid = [w for w in limits._windows(quota) if _when(w.get('resets_at')) is not None]
        full = [w for w in valid if w['used_percent'] >= 100]
        account = quota.get('limit_id') or e['harness']
        for w in valid:
            quiet.append(dict(ts=e['ts'], t=t, harness=e['harness'], account=account, plan_type=quota.get('plan_type'), window=(w['minutes'], w['resets_at']),
                              used_percent=w['used_percent'], reached=quota.get('reached'), turn=None, who=None, i=None, resets=w['resets_at'], due=_when(w['resets_at']), event=True,
                              root=(e['harness'], e.get('parent_session') or e.get('session')), hit=_window_hit(quota.get('reached'), full, w),
                              key=(e['harness'], account, w['minutes'], None, quota.get('plan_type'), 0, 0)))
    if not carriers and not rows:
        if quiet:
            quiet.sort(key=_by_time)
            _segment(quiet)
            out.events = quiet
        return out
    pool = [(i, r) for i, r in enumerate(records) if r['harness'] in harnesses]
    if assigned is None:
        assigned = dict(zip((i for i, _ in pool), prompts.assign_prompts([r for _, r in pool])))
    carrying = set(carriers)
    requests = {}
    for i, r in pool:
        a = assigned[i] if isinstance(assigned, list) else assigned.get(i)
        who = tuple(a[:3]) if a else (r['harness'], r['session'], None)
        t = _when(r['ts'])
        if t is None:
            continue
        x = requests.setdefault(who, [t, t, []])
        x[0], x[1] = min(x[0], t), max(x[1], t)
        x[2].append((t, i))
        if i in carrying:
            quota = r['quota']
            valid = reading[i]
            full = [w for w, _ in valid if w['used_percent'] >= 100]
            account = quota.get('limit_id') or r['harness']
            for w, due in valid:
                out.append(dict(ts=r['ts'], t=t, harness=r['harness'], account=account, plan_type=quota.get('plan_type'),
                                window=(w['minutes'], w['resets_at']), used_percent=w['used_percent'], reached=quota.get('reached'),
                                turn=who if who[2] is not None else None, who=who, i=i, resets=w['resets_at'], due=due, root=(r['harness'], r.get('parent_session') or r['session']),
                                hit=_window_hit(quota.get('reached'), full, w),
                                key=(r['harness'], account, w['minutes'], None, quota.get('plan_type'), 0, 0)))
    if rows:
        for s in claude_snapshots(claude, records, assigned, rows):
            out.append(s)
            if s['who'] not in requests:
                requests[s['who']] = [s['t'], s['t'], []]  # an unattributed reading with no request of its own
    out.sort(key=_by_time)
    everything = sorted([*out, *quiet], key=_by_time)
    _segment(everything)
    _split_counters(everything)
    out.events = [x for x in everything if x.get('event')]
    out.requests = requests
    out.bearing = frozenset(s['who'] for s in out)
    return out


_by_time = itemgetter('t')


def _split_counters(snapshots):
    """Two counters can share a limit id, plan and reset time (several accounts of one plan): their readings then interleave, one near 99 while
    the other is near 60 (measured on real Codex data; a turn then got nothing from the movement it caused). A session reports one account's
    counter, so counters are told apart by evidence about sessions, never by a jump. Each new reading is compared with the latest reading of
    each of the (up to eight) other sessions that reported within CONFLICT_WINDOW; one more than COUNTER_GAP points away is a conflict, counted
    once per reading for that pair. A pair is incompatible when both sessions have at least CONFLICT_EACH such readings (both keep reporting
    their own level), and an agreeing reading clears the pair's count (noise between sessions of one account is transient). A lone jump, or
    one from a session that started after the other stopped, never splits. Sessions (a subagent counts as its parent's) are
    placed, in order of first reading, on the first counter that holds none they are incompatible with. Counters are numbered from 0."""
    by = {}
    for s in snapshots:  # time order
        if not s.get('straggler'):  # a slower session's reading of the counter from before a reset is no evidence about accounts
            by.setdefault(s['key'], []).append(s)
    for rows in by.values():
        who = {id(s): s['root'] for s in rows}
        if len(set(who.values())) < 2:
            continue
        recent, pairs, apart = OrderedDict(), {}, set()  # recent: session -> (time, value), oldest first
        for s in rows:
            me, v, t = who[id(s)], s['used_percent'], s['t']
            for other, (to, vo) in islice(reversed(recent.items()), 9):
                if other == me:
                    continue
                if t - to > CONFLICT_WINDOW:
                    break
                pair = (me, other) if me < other else (other, me)
                state = pairs.setdefault(pair, {})
                if abs(v - vo) > COUNTER_GAP:
                    state[me] = state.get(me, 0) + 1
                    if len(state) == 2 and min(state.values()) >= CONFLICT_EACH:
                        apart.add(pair)
                else:
                    state.clear()  # they agree again: transient noise between sessions of one account
            recent[me] = (t, v)
            recent.move_to_end(me)
        if not apart:
            continue
        counters, home = [], {}
        for s in rows:
            me = who[id(s)]
            if me in home:
                continue
            for n, members in enumerate(counters):
                if not any(((me, m) if me < m else (m, me)) in apart for m in members):
                    break
            else:
                counters.append([])
                n = len(counters) - 1
            counters[n].append(me)
            home[me] = n
        for s in rows:
            n = home[who[id(s)]]
            if n:
                s['key'] = (*s['key'][:5], n, s['key'][6])


def _segment(snapshots):
    """Window instances. A series (harness, account, plan, length) is one continuous window: Codex reset times slide (limits.py treats them so),
    so a changed `resets_at` alone does not start a new instance. A new instance starts on evidence of a discontinuity only: a confirmed reset,
    that is a reset time that moved forward by more than half the window (a real reset moves it by about the window length; drift is minutes)
    with the reading made after the previous instance's reset time, whichever way the counter went, or with a lower reading, whatever the drop; a drop of the counter big enough on its own (RESET_DROP, RESET_RATIO); or no reading
    for longer than the window itself. For STRAGGLE after a reset, a reading back at the old level is a slower session still reporting the old
    counter: it stays with the old instance and is flagged `straggler`. A lower reading otherwise is a stale one. The key's reset time is the
    latest one reported, as context; the instance number keeps instances of one series apart. Counters are split afterwards, inside an instance."""
    by = {}
    for s in snapshots:
        by.setdefault((*s['key'][:3], s['key'][4]), []).append(s)
    for rows in by.values():
        minutes = rows[0]['key'][2]
        gap, half = timedelta(minutes=minutes), timedelta(minutes=minutes / 2)
        groups, top, last, reset_at, old_top, resets = [], None, None, None, None, None
        for j, s in enumerate(rows):
            v, due = s['used_percent'], s['due']
            if groups and reset_at is not None and s['t'] - reset_at <= STRAGGLE and v >= old_top - STRAGGLE_POINTS and v > top:
                s['straggler'] = True
                groups[-2].append(s)  # a slower session still reporting the counter from before the reset
                last = s['t']
                continue
            if last is None or s['t'] - last > gap:
                groups.append([])
                top, reset_at, resets = v, None, due
            elif (due - resets > half and s['t'] >= resets) or (v < top and ((due - resets > half) or (top - v > RESET_DROP and v < top * RESET_RATIO and not _returns(rows, j, top)))):
                groups.append([])
                old_top, top, reset_at, resets = top, v, s['t'], due
            else:
                top = max(top, v)
                resets = max(resets, due) if due - resets <= half else resets  # drift only moves it a little
            groups[-1].append(s)
            last = s['t']
        for n, group in enumerate(groups):
            if group:
                latest = group[-1]['resets']
                for s in group:
                    s['window'] = (s['window'][0], latest)
                    s['key'] = (*s['key'][:3], latest, s['key'][4], 0, n)


def _returns(rows, j, top):
    """Does the counter come back up to the old level after a drop at rows[j], once the stragglers' STRAGGLE has passed? Then the drop is not a
    reset but a second counter (or a stale reading) interleaved with this one."""
    t0 = rows[j]['t']
    for s in rows[j + 1:]:
        if s['t'] - t0 > CONFLICT_WINDOW:
            return False
        if s['t'] - t0 > STRAGGLE and s['used_percent'] >= top - STRAGGLE_POINTS:
            return True
    return False


def _series(key):
    """A window instance's key without its reset time and number: (harness, account, minutes, plan). The plan type is part of the identity because
    several ChatGPT accounts can share a limit id and even a reset time while their counters differ (measured: pro, team and prolite in one instance)."""
    return (*key[:3], key[4])


def _instances(snapshots):
    """{window key: its snapshots in time order}."""
    found = {}
    for s in sorted(snapshots, key=_by_time):  # timsort: linear on the usual, already ordered input
        found.setdefault(s['key'], []).append(s)
    return found


def _percent_first(s):
    return s['used_percent']


def _table_hit(hits, key, rows, peak):
    """True when limits.limit_hits names a hit of this window length inside the instance's readings, of the same series (harness, limit id, plan)
    and from a session of the same counter; None when a hit that names no window (both windows full, a reached type with no single full window)
    falls inside and this window is at 100 %; else False."""
    harness, account, minutes, _, plan = key[:5]
    first, last, roots = rows[0]['t'], rows[-1]['t'], {s['root'] for s in rows}
    dues = [s['due'] for s in rows]
    unnamed = False
    for h in hits:
        if h['harness'] != harness or h.get('reached') is None or 'credits' in str(h['reached']):
            continue
        origin = h.get('origin')
        if origin and ((origin['limit_id'] or harness) != account or origin['plan_type'] != plan or (harness, origin['session']) not in roots):
            continue  # another limit, another plan or another counter
        at = _when(h['at'])
        if at is None or at > last:
            continue
        if at < first:  # a hit that started windowless and learned its window keeps its first time: up to a window before the first reading,
            reset = _when(h.get('resets_at'))  # for the same reset time
            if h.get('window_minutes') != minutes or reset is None or at < first - timedelta(minutes=minutes) \
                    or not min(dues) - timedelta(minutes=10) <= reset <= max(dues) + timedelta(minutes=10):
                continue
        if h.get('window_minutes') == minutes:
            return True
        unnamed = unnamed or h.get('window_minutes') is None
    return None if unnamed and peak >= 100 else False


@_nogc
def windows(snapshots, last=None, records=None, cost_of=None, hits=None):
    """One entry per window instance, oldest reset first: {harness, account, plan_type, minutes, resets_at, start, peak_percent, peak_at,
    first_percent, snapshots, hit}. Quota-only events (`snapshots.events`) count for peak, first reading and `snapshots`. `hit` is derived from
    `hits` (limits.limit_hits) when given: True when a hit of this length of this harness falls inside the instance's readings, None when one that
    names no window does and the window is at 100 %, else False; without `hits`, from the readings alone: a reached limit (not depleted credits)
    while this is the only window at 100 % (None when several are). `last` keeps only the most recent that many windows of each (harness, account,
    length, plan). With `records` and `cost_of` (record -> list-price USD or None), the windows also get `cost`: the list price of the requests that
    reported the window (each in its own instance) plus those of the same sessions that did not report it, placed in the instance of that session's
    nearest reading; `unpriced_requests`; and `uncertain_requests`: requests of sessions that never reported any window, which may or may not have
    used it and are left out (`lower_bound` is then true, as it is for incomplete token counters). Cost is computed after `last`, so only the
    windows kept are priced."""
    events = list(getattr(snapshots, 'events', ()))
    main = _instances(snapshots)
    everything = _instances([*snapshots, *events]) if events else main
    out, rows_of, keys_of = [], {}, {}
    for key, rows in everything.items():
        harness, account, minutes, resets_at, plan = key[:5]
        peak = max(rows, key=_percent_first)
        if hits is not None:
            hit = _table_hit(hits, key, rows, peak['used_percent'])
        else:
            hit = True if any(s['hit'] is True for s in rows) else None if any(s['hit'] is None for s in rows) else False
        entry = dict(harness=harness, account=account, plan_type=plan, minutes=minutes, resets_at=resets_at,
                     start=(prompts._t(resets_at) - timedelta(minutes=minutes)).isoformat(), peak_percent=peak['used_percent'], peak_at=peak['ts'],
                     first_percent=rows[0]['used_percent'], snapshots=len(rows), hit=hit)
        rows_of[id(entry)], keys_of[id(entry)] = main.get(key, []), key
        out.append(entry)
    out.sort(key=lambda w: (prompts._t(w['resets_at']), w['minutes'], w['harness'], w['account'], w['plan_type'] or ''))
    if last is not None:
        groups = {}
        for w in out:
            groups.setdefault((w['harness'], w['account'], w['minutes'], w['plan_type']), []).append(w)
        keep = {id(w) for rows in groups.values() for w in rows[-last:]}
        out = [w for w in out if id(w) in keep]
    if records is not None and cost_of is not None and out:
        requests = getattr(snapshots, 'requests', None)
        root = lambda r: (r['harness'], r.get('parent_session') or r['session'])
        kept = {keys_of[id(w)] for w in out}
        kept_series = {_series(k) for k in kept}
        seen, claimed, nearest, kin = set(), set(), {}, {}  # kin: (harness, account, plan) -> sessions that report any of its windows
        for s in snapshots:
            seen.add(s['root'])
            kin.setdefault((*s['key'][:2], s['key'][4]), set()).add(s['root'])
            sr = _series(s['key'])
            if sr in kept_series:
                if s['i'] is not None:
                    claimed.add((sr, s['i']))
                times, keys = nearest.setdefault((sr, s['root']), ([], []))
                times.append(s['t'])
                keys.append(s['key'])
        due_of = {key: rows[-1]['due'] for key, rows in main.items()}  # an instance's latest reset time: a request after it is not in the instance
        # an instance covers the time from its first reading, or one window before its reset if that is earlier, to its reset
        since = {key: min(rows[0]['t'], rows[-1]['due'] - timedelta(minutes=key[2])) for key, rows in main.items()}
        spans_of = {}  # series -> [(first reading, last reading, key)] of every instance
        for key, rows in main.items():
            if _series(key) in kept_series:
                spans_of.setdefault(_series(key), []).append((rows[0]['t'], rows[-1]['t'], key))
        for lst in spans_of.values():
            lst.sort()
        members = {id(w): list(dict.fromkeys(s['i'] for s in rows_of[id(w)] if s['i'] is not None)) for w in out}  # the requests that reported this window instance (several readings can follow one request)
        placed = {}  # series -> requests already in an instance: a request belongs to one instance per window length
        for w in out:
            taken = placed.setdefault(_series(keys_of[id(w)]), set())
            members[id(w)] = [i for i in members[id(w)] if i not in taken]
            taken.update(members[id(w)])
        uncertain = {id(w): 0 for w in out}
        by_key = {keys_of[id(w)]: w for w in out}

        def put(key, i, fits):
            w = by_key.get(key)
            if w is not None:
                if fits:
                    members[id(w)].append(i)
                else:
                    uncertain[id(w)] += 1

        if requests is not None:
            mine, unseen = {}, {}  # a reporting session's requests; the requests of sessions that never reported any window
            harnesses = {w['harness'] for w in out}
            listed = sorted((x for who, (_, _, l) in requests.items() if who[0] in harnesses for x in l), key=_first)
            for t, i in listed:
                r = records[i]
                if r.get('id_synthetic'):
                    continue
                who = root(r)
                (mine if who in seen else unseen).setdefault(who, []).append((t, i))
            # Every request of a session that reported a series belongs to one instance of it, by the time: the instances of the session whose
            # span [reset - window, reset] (from its first reading if earlier) contains the request are the candidates, and exactly one must.
            # None, or several, and the request is counted as uncertain for the instance of the session's nearest reading.
            for (sr, who), (times, keys) in nearest.items():
                distinct = list(dict.fromkeys(keys))
                for t, i in mine.get(who, []):
                    if (sr, i) in claimed:
                        continue
                    covering = [k for k in distinct if since[k] <= t <= due_of[k]]
                    if len(covering) == 1:
                        put(covering[0], i, True)
                        continue
                    n = bisect_left(times, t)
                    best = 0 if n == 0 else n - 1 if n == len(times) else (n - 1 if t - times[n - 1] <= times[n] - t else n)
                    put(keys[best], i, False)
            # A session that reports a sibling window of the account and plan (a 5-hour-only session) but not this series has its requests
            # placed in the instance of the series that covers their time: by the readings' span, else by the reset window.
            for sr, instances in spans_of.items():
                gap = timedelta(minutes=sr[2])
                starts = [x[0] for x in instances]
                for who in kin.get((*sr[:2], sr[3]), ()):
                    if (sr, who) in nearest:
                        continue
                    for t, i in mine.get(who, []):
                        n = bisect_right(starts, t) - 1
                        for first, last_, key in instances[max(n, 0):n + 2]:
                            if first <= t <= last_ or due_of[key] - gap <= t <= due_of[key]:
                                put(key, i, True)
                                break
            for w in out:
                rows = rows_of[id(w)]
                if rows:  # sessions that never reported any window may or may not have used this one
                    for reqs in unseen.values():
                        uncertain[id(w)] += bisect_right(reqs, rows[-1]['t'], key=_first) - bisect_left(reqs, rows[0]['t'], key=_first)
        for w in out:
            ids = members[id(w)]
            costs = [cost_of(records[i]) for i in ids if not records[i].get('id_synthetic')]  # ambiguous identities are in no total
            priced = [c for c in costs if c is not None]
            w['cost'] = sum(priced) if priced else None
            w['unpriced_requests'] = len(costs) - len(priced)
            w['uncertain_requests'] = uncertain[id(w)]
            w['lower_bound'] = uncertain[id(w)] > 0 or any(not records[i].get('complete', True) for i in ids)
    return out


_first = itemgetter(0)


def _weight(record):
    """A request's size for weighing it against a priced one: fresh input, cache writes and output, and cache reads at a tenth."""
    t = record.get('tokens') or {}
    return (t.get('fresh_input') or 0) + (t.get('cache_write') or 0) + (t.get('output') or 0) + 0.1 * (t.get('cache_read') or 0)


@_nogc
def turn_shares(records, snapshots, table=None, cost_of=None, only=None):
    """{turn: {window minutes: share}} with turn = (harness, session, turn_id) and share = {window_key, observed: {before, after, delta,
    shared_with} or None, estimate: float or None, lower, upper, label: 'observed' | 'estimate' | 'range' | 'unknown'}, for every window length
    the turn has snapshots in.

    A window's movement is walked once. Each step (the snapshots at one time, after the previous time) moved the account-wide counter by
    pct - previous pct. Everyone who may have moved it takes part: turns, usage that no turn owns (a pseudo-turn per session, never returned),
    and requests without any snapshot. A participant is active in a step when the span from its first to its last request (with a snapshot or
    not) meets the step's interval. A turn's share is a RANGE that needs no prices: `lower` is the movement in the steps where it was the only
    active participant, `upper` all the movement in the steps where it was active at all, so whatever split of a shared step is true lies
    between them (the lowers add up to at most the movement, the uppers to at least the movement). It is 'observed' when lower == upper (alone
    in every step that moved) and its first and last requests both carry a snapshot of the window. Otherwise it is a 'range'; when the range
    is at most NARROW points wide it also has a point, `estimate`: the shared steps split by what each participant's own requests in the step
    cost (an unpriced request weighs its tokens at the instance's average list price per token; with no priced request in the instance, or no
    cost in a step at all, there is no point). A turn is unknown only without a valid `before` (no reading before its first request) or when it
    crossed into a new instance of the same window (a reset). `shared_with` counts the other participants whose spans overlap its own,
    symmetrically. A lower reading is a stale one (see RESET_DROP) and moves nothing. `cost_of` (record -> USD or None; default prompts._cost
    with `table`) lets a caller reuse costs; ambiguous (id_synthetic) requests weigh 0. `only` (a set of turn keys) skips the work for steps
    in which none of them is active."""
    price = cost_of or (lambda r: prompts._cost(r, table))
    costs = {}

    def cost(i):
        if i not in costs:
            r = records[i]
            costs[i] = 0.0 if r.get('id_synthetic') else price(r)  # None: unpriced, which is not the same as free
        return costs[i]

    requests = getattr(snapshots, 'requests', None)
    if requests is None:  # plain snapshot lists carry only the requests that have a snapshot
        requests = {}
        for s in sorted(snapshots, key=_by_time):
            x = requests.setdefault(s['who'], [s['t'], s['t'], []])
            x[1] = s['t']
            x[2].append((s['t'], s['i']))
    bearing = {s['who'] for s in snapshots}
    here, kin = {}, {}  # who reports a series; who reports any window of an account's plan (the series' siblings, e.g. the 5-hour one)
    for s in snapshots:
        here.setdefault(_series(s['key']), set()).add(s['who'])
        kin.setdefault((*s['key'][:2], s['key'][4]), set()).add(s['who'])
    nowhere = [w for w in requests if w not in bearing]  # participants whose requests carry no snapshot at all
    ledger = {}
    sessions = {}  # (harness, session) -> the participants (turns) that ran in it
    for who in requests:
        sessions.setdefault(who[:2], []).append(who)

    def books(who):
        """A participant's requests by time with running totals: (times, priced cost so far, unpriced requests so far)."""
        if who not in ledger:
            items = sorted((t.timestamp(), i) for t, i in requests[who][2])
            money, missing, tokens = [0.0], [0], [0.0]
            for _, i in items:
                c = cost(i)
                money.append(money[-1] + (c or 0.0))
                missing.append(missing[-1] + (c is None))
                tokens.append(tokens[-1] + (_weight(records[i]) if c is None else 0.0))
            ledger[who] = ([t for t, _ in items], money, missing, tokens)
        return ledger[who]

    def in_step(who, tp, tc):  # (priced cost, unpriced requests, their token weight) of the participant's requests in (tp, tc]
        times, money, missing, tokens = books(who)
        lo, hi = bisect_right(times, tp), bisect_right(times, tc)
        return money[hi] - money[lo], missing[hi] - missing[lo], tokens[hi] - tokens[lo]

    per_turn = {}  # (turn, series) -> {key: result}
    reporters = {}  # counter series -> sessions that reported it in this or an earlier instance
    for key, rows in _instances(snapshots).items():
        groups, times, pcts = [], [], []  # pcts: the highest value seen: a lower reading is a stale one
        for s in rows:
            if times and s['t'] == times[-1]:
                groups[-1].append(s)
                pcts[-1] = max(pcts[-1], s['used_percent'])
            else:
                groups.append([s])
                times.append(s['t'])
                pcts.append(max(s['used_percent'], pcts[-1]) if pcts else s['used_percent'])
        mine = {}
        for s in rows:
            mine.setdefault(s['who'], []).append(s)
        spans = {w: (requests[w][0].timestamp(), requests[w][1].timestamp()) for w in mine}
        for w, rs in mine.items():  # a reading follows its request: the participant is active until its last reading (request times stay as they are)
            spans[w] = (spans[w][0], max(spans[w][1], rs[-1]['t'].timestamp()))
        first, last = rows[0]['t'].timestamp(), rows[-1]['t'].timestamp()
        # competitors that do not report this window but may draw on its counter: turns that only report a sibling window of the same account
        # and plan (a 5-hour-only turn in the weekly window), and requests that report no window at all
        series = _series(key)
        known = reporters.setdefault((*series, key[5]), set())
        known.update(s['root'] for s in rows)
        # and any turn of a session that reported this counter in this or an earlier instance, whether or not it has a reading in this instance
        # (readings lost after a reset leave the turn running without them)
        for w in [*(kin.get((*key[:2], key[4]), ()) - here.get(series, set())), *(x for x in nowhere if x[0] == key[0]),
                  *(x for session in known for x in sessions.get(session, ()))]:
            lo, hi = requests[w][0].timestamp(), requests[w][1].timestamp()
            if w not in spans and hi >= first and lo <= last:
                spans[w] = (lo, hi)
        # An unpriced request weighs its tokens at the instance's average list price per token of its priced requests (none priced: no rate).
        priced_cost = priced_tokens = 0.0
        for i in {s['i'] for s in rows if s['i'] is not None}:
            if not records[i].get('id_synthetic') and cost(i) is not None:
                priced_cost += cost(i)
                priced_tokens += _weight(records[i])
        rate = priced_cost / priced_tokens if priced_cost > 0 and priced_tokens > 0 else None
        lower, upper, point, estimated, nopoint = {}, {}, {}, set(), set()  # per participant: sole-step movement, all active-step movement, cost-split movement
        for g in range(1, len(groups)):
            move = pcts[g] - pcts[g - 1]
            if not move:
                continue
            tp, tc = times[g - 1].timestamp(), times[g].timestamp()
            active = [w for w, (lo, hi) in spans.items() if lo <= tc and hi > tp]
            if not active:
                continue
            if only is not None and not (only & set(active)):
                continue
            for w in active:
                upper[w] = upper.get(w, 0.0) + move  # the most any participant active in the step can have had of it: all of it
            if len(active) == 1:
                w = active[0]
                lower[w] = lower.get(w, 0.0) + move  # the least: what moved while it was the only one active
                point[w] = point.get(w, 0.0) + move
                continue
            # A shared step: the bounds need no prices. The point (a split by what each participant's own requests in the step cost) is only
            # as good as the prices: an unpriced request is weighed by its tokens at the instance's average price, or not at all.
            weights, missing = {}, False
            for w in active:
                money, lost, lost_tokens = in_step(w, tp, tc)
                missing = missing or (lost > 0 and rate is None)
                weights[w] = money + (rate or 0.0) * lost_tokens
            total = sum(weights.values())
            for w, weight in weights.items():
                estimated.add(w)
                if missing or total <= 0:
                    nopoint.add(w)
                else:
                    point[w] = point.get(w, 0.0) + move * weight / total
        starts = sorted(lo for lo, _ in spans.values())
        ends = sorted(hi for _, hi in spans.values())
        for turn, rs in mine.items():
            if turn[2] is None or (only is not None and turn not in only):
                continue
            lo, hi = requests[turn][0], requests[turn][1]
            gi = bisect_left(times, lo)
            a, b = spans[turn]
            shared = bisect_right(starts, b) - bisect_left(ends, a) - 1
            share = dict(window_key=key, observed=None, estimate=None, label='unknown', lower=None, upper=None)
            if gi > 0:
                low, high = lower.get(turn, 0.0), upper.get(turn, 0.0)
                share['observed'] = dict(before=pcts[gi - 1], after=pcts[bisect_right(times, max(hi, rs[-1]['t'])) - 1], delta=low, shared_with=shared)
                share.update(lower=low, upper=high)
                # both the first and the last request carry a snapshot of the window (a Claude reading follows its request: `at` is that request's time)
                first_ok, last_ok = rs[0].get('at', rs[0]['t']) == lo, rs[-1].get('at', rs[-1]['t']) == hi
                if not last_ok:
                    # a request after the last reading may have moved the counter further, so no upper bound is known: only "at least the lower"
                    if floor_pct(low) < 1:
                        share.update(observed=None, lower=None, upper=None)  # and a lower bound that floors to nothing is no information: unknown
                    else:
                        share.update(label='range', upper=None)
                elif turn not in estimated:
                    if first_ok:
                        share['label'] = 'observed'
                    else:
                        share.update(estimate=low, label='estimate')  # alone in every step, but a request without a reading may hide movement
                elif turn not in nopoint and high - low <= NARROW:
                    share.update(estimate=point.get(turn, 0.0), label='estimate')
                else:
                    share['label'] = 'range'
            per_turn.setdefault(turn, {}).setdefault(_series(key), {})[key] = share
    result = {}
    for turn, series in per_turn.items():
        for group in series.values():
            # a turn that ended in a later instance of the window than it started in crossed a reset: unknown
            share = max(group.values(), key=lambda x: x['window_key'][6])
            if len(group) > 1:
                share = dict(window_key=share['window_key'], observed=None, estimate=None, label='unknown', lower=None, upper=None)
            slot = result.setdefault(turn, {})
            minutes = share['window_key'][2]
            if minutes not in slot or _rank(share) > _rank(slot[minutes]):
                slot[minutes] = share
    return result


def _rank(share):
    """What ranks a share: its upper bound (an observed share's value; a one-sided share's lower bound), -1 when unknown."""
    if share['label'] == 'unknown' or share.get('lower') is None:
        return -1
    return share['lower'] if share.get('upper') is None else share['upper']


def largest(shares):
    """{turn: share} from turn_shares: the window where the turn can have moved the counter most (upper bound), then where it surely moved it most (lower), then the weekly one. A turn whose best known share
    is 0 while another of its windows is unknown shows the unknown one: 0 of one limit says nothing about the other."""
    def pick(by):
        best = max(by.values(), key=lambda x: (_rank(x), x.get('lower') or 0, x['window_key'][2]))  # the most it can have used, then the most it surely did, then the weekly one
        if _rank(best) == 0:
            unknown = [x for x in by.values() if x['label'] == 'unknown']
            if unknown:
                return max(unknown, key=lambda x: x['window_key'][2])
        return best
    return {turn: pick(by) for turn, by in shares.items()}


def window_name(minutes):
    """A window is named by its length, never by its slot."""
    return '5-hour' if minutes == FIVE_HOURS else 'weekly' if minutes == WEEK else f'{minutes}-minute'


def value(share):
    """The single percentage a share stands for: the observed delta, or the narrow estimate; None for a range and for unknown."""
    if share['label'] == 'observed':
        return share['observed']['delta']
    return share['estimate'] if share['label'] == 'estimate' else None


def bounds(share):
    """(lower, upper) in points, or None when unknown; upper is None for a one-sided share ("at least the lower": a request after the last reading)."""
    return None if share['label'] == 'unknown' or share.get('lower') is None else (share['lower'], share['upper'])


def auto_evidence(calibration):
    """'5 limit hits', '12 statusline readings' or '5 limit hits and 12 statusline readings': the evidence size behind an automatic budget."""
    by = calibration.get('by_source') or {}
    parts = [f"{n} {word}{'s' * (n != 1)}" for key, word in (('limit_hit', 'limit hit'), ('statusline', 'statusline reading')) if (n := by.get(key))]
    return ' and '.join(parts) or 'your history'


EPS = 1e-6  # float noise: 3.0000000001 is 3, not "more than 3"


def floor_pct(x):
    """The lower end of a range as displayed: rounded DOWN, so the display never claims more than the evidence."""
    return int(math.floor(x + EPS))


def ceil_pct(x):
    """The upper end of a range as displayed: rounded UP, for the same reason."""
    return int(math.ceil(x - EPS))


def range_text(lower, upper):
    """'2–28%', '< 1%–28%', '< 1%' or '3%': whole points, never decimals. The lower end is floored and the upper end ceiled, so the range
    always contains what the readings allow (a point estimate or an observed share rounds normally)."""
    if upper < 1 - EPS:
        return '< 1%'
    lo, up = floor_pct(lower), ceil_pct(upper)
    return f'{up}%' if lo == up else f'< 1%–{up}%' if lo < 1 else f'{lo}–{up}%'


def wide(lower, upper):
    """The one rule for 'is it a range': the displayed endpoints (floored lower, ceiled upper) differ (the page compares them the same way)."""
    return ceil_pct(upper) - floor_pct(lower) >= 1


def _shared_note(n):
    return f" (shared with {n} turn{'s' * (n != 1)})" if n else ''


def _percent(label, v):
    if label not in ('observed', 'estimate', 'calibrated', 'auto-calibrated') or v is None:
        return 'n/a'
    whole = int(v + 0.5)
    return '< 1%' if whole < 1 else f"{'~' if label == 'observed' else '≈'}{whole}%"


def percent_text(share):
    """'~3%' (observed), '< 1%', '≈2%' (estimate), or 'n/a'; whole percent, never decimals."""
    return _percent(share['label'], value(share))


def bare(label, point, lower, upper):
    """A bounded share without its limit: '2–28%', '< 1%–28%', '≥ 4%' (no upper bound), or '≈9% (7–11%)' (a narrow range with its point; the
    point is left out when it rounds to under 1%). The page applies the same rules."""
    if upper is None:
        return f'≥{floor_pct(lower)}%'
    if label == 'estimate' and point is not None and int(point + 0.5) >= 1 and wide(lower, upper):
        return f"{_percent('estimate', point)} ({range_text(lower, upper)})"
    return range_text(lower, upper)


def _bounded(item, name):
    """A shared turn (#131): the least and the most it can have used, never a cost-weighted point; a narrow range also gives its point, unless
    that rounds to under 1% (then the range alone). One-sided ("at least"): a request after the last reading may have moved the counter further."""
    lo, up = item['lower_percent'], item['upper_percent']
    if up is None and floor_pct(lo) < 1:
        return f'share of {name}: n/a'
    return f"{bare(item['label'], item.get('delta_percent'), lo, up)} of {name}{_shared_note(item.get('shared_with'))}"


def line(item, harness):
    """For `top`, from the `quota_share` JSON of a turn (as_json): '~3% of weekly Codex limit', '≈2% of weekly Codex limit (estimate)',
    '< 1% of ...', or 'share of ...: n/a'."""
    name = f"{window_name(item['window_minutes'])} {harness.capitalize()} limit"
    if item['label'] == 'auto-calibrated' and item.get('unfit'):
        return 'share unknown: the automatic estimate does not fit this turn'
    if item['label'] == 'range' or (item['label'] == 'estimate' and item.get('lower_percent') is not None and item.get('upper_percent') is not None
                                    and wide(item['lower_percent'], item['upper_percent'])):
        return _bounded(item, name)
    if item['label'] not in ('observed', 'estimate', 'calibrated', 'auto-calibrated') or item['delta_percent'] is None:
        return f'share of {name}: n/a'
    if item['label'] == 'auto-calibrated':  # from budgets tokenatlas derived itself (budget.py, #116), never an observation
        if item.get('unfit'):  # more than a whole window: the estimate does not fit this turn
            return 'share unknown: the automatic estimate does not fit this turn'
        note = 'estimated from ' + auto_evidence(item['calibration'])
        v = item.get('exact_percent', item['delta_percent'])
        if item.get('lower_bound'):
            floor = int(v)
            return f"≥{floor}% of the {name} ({note})" if floor >= 1 else f"share of the {name}: unknown (some requests are unpriced, incomplete or ambiguous; {note})"
        return f"{_percent('calibrated', v)} of the {name} ({note})"
    if item['label'] == 'calibrated':  # from the user's own calibration (budget.py), never an observation
        note = f"your calibration, {item['calibration']['date']}"
        v = item.get('exact_percent', item['delta_percent'])  # full precision until flooring or rounding
        if item.get('lower_bound'):  # unpriced, incomplete or ambiguous requests are left out: only a floor, and never '< 1%'
            floor = int(v)
            return f"≥{floor}% of your {name} ({note})" if floor >= 1 else f"share of your {name}: unknown (some requests are unpriced, incomplete or ambiguous; {note})"
        return f"{_percent('calibrated', v)} of your {name} ({note})"
    return f"{_percent(item['label'], item['delta_percent'])} of {name}" + (' (estimate)' if item['label'] == 'estimate' else '')


def text(share):
    return line(as_json(share), share['window_key'][0])


def _down(x):
    """Two decimals, rounded down: a bound is never made to claim more than it is (3.999 stays 3.99)."""
    return math.floor(x * 100 + EPS) / 100


def _up(x):
    return math.ceil(x * 100 - EPS) / 100


def as_json(share):
    obs = share['observed']
    v, b = value(share), bounds(share)
    return dict(window_minutes=share['window_key'][2], delta_percent=None if v is None else round(v, 2) if share['label'] == 'estimate' else v,
                lower_percent=b and _down(b[0]), upper_percent=_up(b[1]) if b and b[1] is not None else None,
                label=share['label'], before=obs and obs['before'], after=obs and obs['after'], shared_with=obs and obs['shared_with'])


def compute(records, table, cost_of=None, only=None, claude=None, assigned=None):
    """(snapshots, {turn: largest-window share}) over all records; ([], {}) when no record carries a quota (`claude`: see snapshots_from_records)."""
    snapshots = snapshots_from_records(records, assigned=assigned, claude=claude)
    return (snapshots, largest(turn_shares(records, snapshots, table, cost_of, only))) if snapshots else ([], {})


def mark_turns(items, shares):
    """Add `quota_share` (as_json, or None) to each ranked turn (dict with harness, session, turn_id)."""
    for item in items:
        share = shares.get((item['harness'], item['session'], item['turn_id']))
        item['quota_share'] = as_json(share) if share else None
    return items
