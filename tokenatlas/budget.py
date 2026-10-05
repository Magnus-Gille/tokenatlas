"""A plan size (manual) or calibration readings (copied from `/usage` or the Codex limits display) turn list price into a share of a limit (#93).

A reading is the used percentage the user read for one window at one time, together with the list-price cost tokenatlas saw for that harness in
the window up to then. budget = cost seen / used fraction is a *list-price size of the window*, not a published number: list price per 1% of a
weekly window varies 2-3x between weeks (docs/quota.md), so several readings are kept and their spread is always shown next to the median.
Usage tokenatlas does not see (chat, other machines, cloud tasks) is in the percentage the user reads but not in the cost seen, so the budget comes out too small and turn shares too large. The file lives next to the history (0600); nothing leaves the machine."""
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import prompts, quota_share

FILE = 'quota-budget.json'
WINDOWS = {'5h': 300, '7d': 10080}
HARNESSES = ('claude', 'codex')
PROVIDER = {'claude': 'anthropic', 'codex': 'openai'}  # the subscription's own provider; Codex can also run other providers (OpenRouter, ...) that do not count against it
MIXED = '\0mixed'  # a turn whose requests ran on more than one plan: no budget fits it
PLANNED = ('codex',)  # harnesses whose quota snapshots name a plan
ROLLING = ('codex',)  # Codex windows are rolling ([reading - window, reading]); Claude's weekly window resets at a fixed time
KEEP = 8  # readings older than this many windows are ignored
AUTO_RISE = 5.0  # automatic statusline points need the percentage to rise by at least this many points (the 1% resolution and noise dominate below)
AUTO_MIN_COST = 0.50  # ... and the window to hold at least this much list-price cost (USD); a limit hit needs the same
AUTO_MIN_POINTS = 3  # an automatic budget is used for shares only with at least this many points ...
AUTO_MIN_WINDOWS = 2  # ... from at least this many distinct windows ...
AUTO_MAX_SPREAD = 4.0  # ... whose largest point is at most this many times the smallest; otherwise it is only listed (`quota show`), with the reason
AUTO = ('limit_hit', 'statusline')  # the sources of automatic points; a budget from both is 'limit_hit+statusline'
DEFAULT_LIMIT = lambda harness, limit_id: limit_id in (None, harness)  # a harness's own limit; a per-model limit (e.g. codex_bengalfox) is another counter
AUTO_HIT_REACHED = {'claude': ('five_hour', 'seven_day'), 'codex': ('window_full',)}


def path_for(db):
    return Path(db).expanduser().with_name(FILE)


def parse_time(text, what):
    """ISO time ('2026-10-09 21:00', 'T' or space, optional offset or Z); without an offset it is the machine's local time, as /usage shows it."""
    try:
        t = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'{what} {text!r} is not a time; use e.g. "2026-10-09 21:00" or 2026-10-09T21:00+02:00') from None
    return (t.astimezone() if t.tzinfo is None else t).astimezone(timezone.utc)


def parse_used(text):
    """'52%' or '52' to the fraction 0.52; the percentage must be above 0 and at most 100."""
    found = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*%?\s*', str(text))
    if not found:
        raise ValueError(f'--used {text!r} is not a percentage; use e.g. 52% (as /usage shows it)')
    percent = float(found[1].replace(',', '.'))
    if not 0 < percent <= 100:
        raise ValueError(f'--used {text!r} must be above 0% and at most 100%')
    return percent / 100


def window_minutes(name):
    if name not in WINDOWS:
        raise ValueError(f'--window {name!r} must be one of {", ".join(WINDOWS)}')
    return WINDOWS[name]


def _empty():
    return dict(version=1, readings=[], budgets=[])


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot read {path}: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('readings', []), list) or not isinstance(data.get('budgets', []), list):
        raise ValueError(f'{path} is not a quota budget file')
    return dict(version=1, readings=data.get('readings', []), budgets=data.get('budgets', []))


def save(path, data):
    """Atomic: a temporary file in the same directory (0600 from the start), then a rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{FILE}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        if os.name != 'nt':
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def plan_of(r):
    """The plan a Codex request ran under, from the quota snapshot on the observation (lower case); None when it carries none. Claude has no plan information."""
    plan = (r.get('quota') or {}).get('plan_type')
    return plan.strip().lower() if isinstance(plan, str) and plan.strip() else None


def multi_counter_plans(records, start=None, end=None):
    """The Codex plans that more than one account's counter was seen on (several accounts of one plan share a limit id, plan and reset time; quota_share's
    counter detection tells them apart by what their sessions report), within [start, end] when given. Their usage cannot be told apart by plan alone."""
    codex = [r for r in records if r['harness'] in PLANNED and plan_of(r) and (start is None or start <= prompts._t(r['ts']) <= end)]
    counters = {}
    for snap in quota_share.snapshots_from_records(codex) if codex else ():
        plan = (snap.get('plan_type') or '').strip().lower()
        counters.setdefault((plan, snap['account'], snap['window'][0]), set()).add(snap['key'][5])
    return frozenset(plan for (plan, _, _), found in counters.items() if len(found) > 1)


def _provider_ok(r, harness, canon):
    return r['harness'] == harness and canon(r.get('provider')) == canon(PROVIDER[harness])


def _canon(table):
    aliases = (table or {}).get('provider_aliases') or {}
    return lambda p: aliases.get(p, p)


def turn_costs(records, assigned, cost_of, table=None):
    """{turn: (list-price USD of its identified requests, lower_bound, plan)} over the whole history, with `assigned` parallel to `records`
    (prompts.assign_prompts) and `cost_of(record) -> USD or None`. Ambiguous (id_synthetic) requests are left out, as in readings and quota_share; a turn with
    an ambiguous, unpriced or incomplete request is a lower bound. Only the harness's subscription provider counts (provider aliases from `table`): a turn on
    another provider has no entry, so it gets no calibrated share. Codex requests are scoped by plan (plan_of): a request without a quota snapshot has an
    unknown plan, is left out and makes the turn a lower bound; a turn spanning plans has plan MIXED, and so does one with a request under another limit id (a per-model limit) and one on a plan with several detected
    accounts (multi_counter_plans): no budget fits it. Claude has no plan (None)."""
    out, other = {}, set()
    multi = multi_counter_plans(records) if any(r['harness'] in PLANNED and plan_of(r) for r in records) else frozenset()
    canon = _canon(table)
    for record, a in zip(records, assigned):
        if not a or record['harness'] not in PROVIDER or not _provider_ok(record, record['harness'], canon):
            continue
        key = tuple(a[:3])
        by_plan, lower = out.setdefault(key, [{}, False])
        plan = plan_of(record) if record['harness'] in PLANNED else None
        if not DEFAULT_LIMIT(record['harness'], (record.get('quota') or {}).get('limit_id')):
            other.add(key)  # it ran under another limit's counter (checked first, whatever its plan or id): no budget of the default limit fits the turn
            continue
        if record.get('id_synthetic') or (record['harness'] in PLANNED and plan is None):
            out[key][1] = True
            continue
        c = cost_of(record)
        if c is None or not record.get('complete', True):
            out[key][1] = True
        by_plan[plan] = by_plan.get(plan, 0.0) + (c or 0.0)
    result = {}
    for key, (by_plan, lower) in out.items():
        if key in other or len(by_plan) > 1 or any(p in multi for p in by_plan):
            result[key] = (sum(by_plan.values()), True, MIXED)
        else:
            plan, cost = next(iter(by_plan.items()), (None, 0.0))
            result[key] = (cost, lower, plan)
    return result


def table_id(table):
    """Identity of a price table: its retrieval date and a short hash of its content. A reading's cost is only valid for the table it was priced with."""
    digest = hashlib.sha256(json.dumps(table, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{(table or {}).get('retrieved_on')}:{digest}"


def window_start(x):
    """Start of the window a stored reading's cost was seen in: trailing for Codex and for a reading without resets, else resets - window."""
    span = timedelta(minutes=x['minutes'])
    if x['harness'] in ROLLING or x.get('resets_at') is None:
        return prompts._t(x['taken_at']) - span
    return prompts._t(x['resets_at']) - span


def reprice(data, table, records_fn=None):
    """`data` with every reading priced for `table`: a reading made with another table is recomputed from the history (`records_fn()`, called at most once);
    without a history it is dropped as stale, and so is one whose window now has no priced cost. Manual budgets do not depend on a table."""
    tid, records, out = table_id(table), None, []
    for x in data.get('readings', []):
        if not _valid(x) or x.get('table') == tid:
            out.append(x)
            continue
        if records_fn is None:
            continue
        if records is None:
            records = records_fn()
        cost, unpriced, excluded = cost_seen(records, table, x['harness'], window_start(x), prompts._t(x['taken_at']), x.get('plan'))
        if cost > 0:
            out.append(dict(x, cost_usd=cost, unpriced_requests=unpriced, excluded_requests=excluded, table=tid))
    return dict(data, readings=out)


def cost_seen(records, table, harness, start, end, plan=None, default_limit=False):
    """(list-price USD, unpriced requests, excluded requests) of one harness's requests on its subscription provider in [start, end]; ambiguous (id_synthetic)
    requests are left out. For Codex only requests of `plan` count (a request on another plan is another account's usage and is ignored); a request without
    a quota snapshot has an unknown plan and is left out and counted as excluded. With `default_limit`, a request that ran under another limit id (a per-model
    limit) is left out too: it is not usage of the default limit's counter."""
    total, unpriced, excluded = 0.0, 0, 0
    canon = _canon(table)
    for r in records:
        if not _provider_ok(r, harness, canon) or r.get('id_synthetic'):
            continue
        t = prompts._t(r['ts'])
        if not start <= t <= end:
            continue
        if default_limit and not DEFAULT_LIMIT(harness, (r.get('quota') or {}).get('limit_id')):
            continue
        if harness in PLANNED:
            p = plan_of(r)
            if p is None:
                excluded += 1
                continue
            if p != plan:
                continue
        c = prompts._cost(r, table)
        if c is None:
            unpriced += 1
        else:
            total += c
    return total, unpriced, excluded


def resolve_plan(records, table, harness, start, taken, plan=None):
    """The plan a reading belongs to: None for Claude (no plan information; --plan is an error). Codex: --plan if given, else the one plan active in the window,
    else the plan of the most recent Codex observation before the reading; several plans in the window without --plan, or none known, is an error."""
    if harness not in PLANNED:
        if plan:
            raise ValueError(f'--plan is only for Codex: {harness} readings carry no plan information')
        return None
    if plan:
        return plan.strip().lower()
    canon = _canon(table)
    mine = [(prompts._t(r['ts']), plan_of(r)) for r in records if _provider_ok(r, harness, canon) and not r.get('id_synthetic') and plan_of(r)]
    active = sorted({p for t, p in mine if start <= t <= taken})
    if len(active) > 1:
        raise ValueError(f'several Codex plans were active in the window ({", ".join(active)}); say which one this reading is for with --plan')
    if active:
        return active[0]
    before = [x for x in mine if x[0] <= taken]
    if before:
        return max(before)[1]
    raise ValueError('no Codex plan is known from the history; pass --plan (e.g. --plan pro)')


def make_reading(records, table, harness, window, used, resets=None, at=None, now=None, plan=None):
    """A validated reading with the cost seen in [resets - window, at] for Claude when --resets is given; otherwise (always for Codex, whose windows are
    rolling and whose reset time moves) in [at - window, at], marked `approximate` only for Claude without --resets."""
    if harness not in HARNESSES:
        raise ValueError(f'--harness {harness!r} must be one of {", ".join(HARNESSES)}')
    minutes, fraction = window_minutes(window), parse_used(used)
    now = now or datetime.now(timezone.utc)
    taken = parse_time(at, '--at') if at else now
    span = timedelta(minutes=minutes)
    rolling = harness in ROLLING
    approximate = resets is None and not rolling
    end = None
    if resets is not None:
        end = parse_time(resets, '--resets')
        if end <= taken:
            raise ValueError(f'--resets {resets!r} is not after the reading time; give the time the window resets next')
        if end - taken > span:
            raise ValueError(f'--resets {resets!r} is more than a {window} window after the reading time')
    start = taken - span if rolling or end is None else end - span
    plan = resolve_plan(records, table, harness, start, taken, plan)
    if plan and plan in multi_counter_plans(records, start, taken):
        raise ValueError(f'more than one {harness} account on the {plan} plan was active in the window (their counters differ), so a calibration cannot tell whose '
                         f'usage the percentage counts; calibration needs a single account. Use `quota set --harness {harness} --window {window} --plan {plan} --budget-usd N` instead')
    cost, unpriced, excluded = cost_seen(records, table, harness, start, taken, plan)
    if cost <= 0:
        raise ValueError(f'tokenatlas saw no priced {harness}{" " + plan + "-plan" if plan else ""} usage between {start.isoformat(timespec="minutes")} and {taken.isoformat(timespec="minutes")}, '
                         'so a budget cannot be derived from this reading; check the harness, the window and the reset time, or refresh the history first, '
                         f'or set a budget directly with `quota set --harness {harness} --window {window} --budget-usd N`' + (f'; {excluded} {harness} requests in the window carry no plan and were left out' if excluded else ''))
    return dict(harness=harness, minutes=minutes, used=fraction, resets_at=None if end is None else end.isoformat(), taken_at=taken.isoformat(),
                cost_usd=cost, unpriced_requests=unpriced, excluded_requests=excluded, approximate=approximate, table=table_id(table), plan=plan)


def add_reading(path, reading):
    """Upsert by (harness, window, taken_at): an identical retry replaces the stored reading instead of adding a second one. Non-dict entries are dropped on write."""
    data = load(path)
    key = lambda x: (x.get('harness'), x.get('minutes'), x.get('plan'), x.get('taken_at'))
    data['readings'] = [x for x in data['readings'] if isinstance(x, dict) and key(x) != key(reading)] + [reading]
    data['budgets'] = [x for x in data['budgets'] if isinstance(x, dict)]
    save(path, data)


def set_budget(path, harness, window, usd, now=None, plan=None):
    if harness not in HARNESSES:
        raise ValueError(f'--harness {harness!r} must be one of {", ".join(HARNESSES)}')
    minutes = window_minutes(window)
    if plan and harness not in PLANNED:
        raise ValueError(f'--plan is only for Codex: {harness} budgets carry no plan')
    plan = plan.strip().lower() if plan else None  # without --plan a Codex budget fits every plan that has none of its own
    if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not usd > 0 or usd == float('inf'):
        raise ValueError(f'--budget-usd {usd!r} must be a positive number')
    data = load(path)
    data['budgets'] = [b for b in data['budgets'] if isinstance(b, dict) and (b.get('harness'), b.get('minutes'), b.get('plan')) != (harness, minutes, plan)]
    data['readings'] = [x for x in data['readings'] if isinstance(x, dict)]  # malformed non-dict entries are dropped on write
    data['budgets'].append(dict(harness=harness, minutes=minutes, plan=plan, budget_usd=float(usd), set_at=(now or datetime.now(timezone.utc)).isoformat()))
    save(path, data)


def forget(path, harness=None, window=None):
    """Delete the readings and budgets matching the filters (all when none); returns how many were removed."""
    minutes = window_minutes(window) if window else None
    if harness is not None and harness not in HARNESSES:
        raise ValueError(f'--harness {harness!r} must be one of {", ".join(HARNESSES)}')
    data = load(path)
    drop = lambda x: not isinstance(x, dict) or (harness is None or x.get('harness') == harness) and (minutes is None or x.get('minutes') == minutes)
    before = len(data['readings']) + len(data['budgets'])
    data['readings'] = [x for x in data['readings'] if not drop(x)]
    data['budgets'] = [x for x in data['budgets'] if not drop(x)]
    removed = before - len(data['readings']) - len(data['budgets'])
    if removed:
        save(path, data)
    return removed


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _aware(text):
    return prompts._t(text).tzinfo is not None  # a stored time without an offset cannot be compared with now


def _plan_ok(x):
    return x.get('plan') is None or (isinstance(x['plan'], str) and bool(x['plan'].strip()))


def _valid(x):
    try:
        return (_aware(x['taken_at']) and (x.get('resets_at') is None or _aware(x['resets_at'])) and x['harness'] in HARNESSES and x['minutes'] in WINDOWS.values() and _num(x['used']) and 0 < x['used'] <= 1 and _num(x['cost_usd']) and x['cost_usd'] > 0 and _plan_ok(x))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _valid_budget(b):
    try:
        return b['harness'] in HARNESSES and b['minutes'] in WINDOWS.values() and _num(b['budget_usd']) and b['budget_usd'] > 0 and _aware(b['set_at']) and _plan_ok(b)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def invalid_entries(data):
    """How many stored readings and budgets are malformed (and so ignored)."""
    return sum(not _valid(x) for x in data.get('readings', [])) + sum(not _valid_budget(x) for x in data.get('budgets', []))


def derive(data, now=None, keep=KEEP):
    """{(harness, window minutes, plan or None): {budget_usd, source: 'manual'|'readings', readings, spread: [min, max], date (YYYY-MM-DD)}}. A manual budget wins; else
    the median of cost seen / used over the readings taken within `keep` windows of now (older ones, and malformed ones, are ignored)."""
    now = now or datetime.now(timezone.utc)
    out = {}
    by = {}
    for x in data.get('readings', []):
        if not _valid(x):
            continue
        taken = prompts._t(x['taken_at'])
        if now - taken > timedelta(minutes=x['minutes'] * keep):
            continue
        by.setdefault((x['harness'], x['minutes'], x.get('plan')), []).append((taken, x['cost_usd'] / x['used']))
    for key, found in by.items():
        values = [v for _, v in found]
        out[key] = dict(budget_usd=statistics.median(values), source='readings', readings=len(values), spread=[min(values), max(values)],
                        date=max(t for t, _ in found).date().isoformat())
    for b in data.get('budgets', []):
        if _valid_budget(b):
            out[(b['harness'], b['minutes'], b.get('plan'))] = dict(budget_usd=float(b['budget_usd']), source='manual', readings=0, spread=None, date=prompts._t(b['set_at']).date().isoformat())
    return out


def _ledger(records, table, harness):
    """The harness's identified, subscription-provider requests as (sorted times, cumulative USD, cumulative unpriced count), for cost between two times by bisect."""
    canon = _canon(table)
    rows = sorted(((prompts._t(r['ts']), prompts._cost(r, table)) for r in records if _provider_ok(r, harness, canon) and not r.get('id_synthetic')), key=lambda x: x[0])
    times, usd, bad = [t for t, _ in rows], [0.0], [0]
    for _, c in rows:
        usd.append(usd[-1] + (c or 0.0))
        bad.append(bad[-1] + (c is None))
    return times, usd, bad


def _between(ledger, start, end, inclusive_start=True):
    """(USD, unpriced requests) in [start, end] (or (start, end])."""
    times, usd, bad = ledger
    i = bisect_left(times, start) if inclusive_start else bisect_right(times, start)
    j = bisect_right(times, end)
    return (usd[j] - usd[i], bad[j] - bad[i]) if j > i else (0.0, 0)


def auto_points(records, hits, snapshots, table):
    """(points, skipped): automatic calibration points from evidence already in the history, never stored. A point is {harness, minutes, plan, source,
    usd (list price of the window at 100%), cost, percent, at (datetime), window_end (datetime)}.
    - `limit_hit`: a Claude rejection (five_hour / seven_day) or a Codex `window_full` hit with a known window says the window was at 100% at `at`;
      the logged list-price cost in the hit's window ([resets_at - window, hit] for Claude, trailing for Codex) is usd, at 1.00. It is only what the logs saw.
    - `statusline`: per Claude window instance, readings are compared with an anchor reading (the first, then each one that moved >= AUTO_RISE points):
      usd = Claude cost after the anchor up to the reading / (rise / 100). Lower readings (a stale idle session) never move the anchor.
    A point whose window holds less than AUTO_MIN_COST, has an unpriced request, runs under another limit id (`other_limit`), has several Claude counters in the window instance (`ambiguous`), has an ambiguous or plan-less Codex request, or whose Codex plan has several
    accounts is skipped and counted in `skipped` by reason. Ambiguous (id_synthetic) requests and other providers are never in a cost."""
    points, skipped = [], {}
    skip = lambda why: skipped.__setitem__(why, skipped.get(why, 0) + 1)
    multi = multi_counter_plans(records) if any(r['harness'] in PLANNED and plan_of(r) for r in records) else frozenset()
    series = {}  # snapshot key (quota_share's window instance and counter split) -> its readings
    for s in snapshots or ():
        if s.get('harness') == 'claude' and s.get('account') == 'claude' and s['window'][0] in WINDOWS.values() and not s.get('straggler'):
            series.setdefault(s['key'], []).append(s)
    instances = {}
    for key in series:
        instances.setdefault((*key[:5], key[6]), []).append(key)  # key[5] is the counter number within the instance
    split = []  # (minutes, start, end) of Claude window instances with more than one account's counter
    for keys in instances.values():
        if len(keys) > 1:
            minutes, due = series[keys[0]][0]['window']
            split.append((minutes, prompts._t(due) - timedelta(minutes=minutes), prompts._t(due)))
    for hit in hits or ():
        harness, minutes, window = hit.get('harness'), hit.get('window_minutes'), hit.get('window')
        if harness not in AUTO_HIT_REACHED or hit.get('reached') not in AUTO_HIT_REACHED[harness] or minutes not in WINDOWS.values():
            continue
        if not window:
            skip('no_window')
            continue
        if not DEFAULT_LIMIT(harness, (hit.get('origin') or {}).get('limit_id')):
            skip('other_limit')  # another limit's counter says nothing about the default one
            continue
        if harness == 'claude' and any(m == minutes and begin < prompts._t(window['end']) and prompts._t(window['start']) < due for m, begin, due in split):
            skip('ambiguous')  # two accounts' counters in this window: its cost cannot be attributed to one of them
            continue
        plan = None
        if harness in PLANNED:
            plan = ((hit.get('origin') or {}).get('plan_type') or '').strip().lower() or None
            if plan is None:
                skip('no_plan')
                continue
            if plan in multi:
                skip('ambiguous')
                continue
        start, end = prompts._t(window['start']), prompts._t(window['end'])
        cost, unpriced, excluded = cost_seen(records, table, harness, start, end, plan, default_limit=True)
        if unpriced or excluded:
            skip('unpriced')
        elif cost < AUTO_MIN_COST:
            skip('little_cost')
        else:
            at = prompts._t(hit['at'])
            resets = hit.get('resets_at')
            points.append(dict(harness=harness, minutes=minutes, plan=plan, source='limit_hit', usd=cost, cost=cost, percent=100.0, at=at,
                               window_end=prompts._t(resets) if resets and not hit.get('rolling') else at))
    ledger = _ledger(records, table, 'claude') if series else None
    for keys in instances.values():
        if len(keys) > 1:
            skip('ambiguous')  # more than one account's counter in the window: the cost between two readings cannot be attributed to one of them
            continue
        found = sorted(series[keys[0]], key=lambda s: s['t'])
        minutes, due = found[0]['window']
        anchor = found[0]
        for s in found[1:]:
            rise = s['used_percent'] - anchor['used_percent']
            if rise < AUTO_RISE:
                continue  # a small or negative movement: the anchor stays
            cost, unpriced = _between(ledger, anchor['t'], s['t'], inclusive_start=False)
            if unpriced:
                skip('unpriced')
            elif cost < AUTO_MIN_COST:
                skip('little_cost')
            else:
                points.append(dict(harness='claude', minutes=minutes, plan=None, source='statusline', usd=cost / (rise / 100), cost=cost, percent=rise, at=s['t'], window_end=prompts._t(due)))
            anchor = s
    return points, skipped


def derive_auto(points, keep=KEEP):
    """{(harness, minutes, plan): {budget_usd, source: 'limit_hit' | 'statusline' | 'limit_hit+statusline', readings (= points), points, windows, spread: [min, max],
    first_date, date (YYYY-MM-DD), by_source, used, not_used}}: `used` is False (with the reason in `not_used`) unless there are AUTO_MIN_POINTS points from AUTO_MIN_WINDOWS windows
    within a spread of AUTO_MAX_SPREAD; combine() leaves such a budget out of the shares. The median of the points in the last `keep` windows (distinct window ends) of each (harness, window length, plan)."""
    by = {}
    for p in points:
        by.setdefault((p['harness'], p['minutes'], p['plan']), []).append(p)
    out = {}
    for key, found in by.items():
        recent = set(sorted({p['window_end'] for p in found})[-keep:])
        found = [p for p in found if p['window_end'] in recent]
        values = [p['usd'] for p in found]
        lo, hi = min(values), max(values)
        if len(values) < AUTO_MIN_POINTS:
            reason = f'not enough evidence yet ({len(values)} of {AUTO_MIN_POINTS} points)'
        elif len(recent) < AUTO_MIN_WINDOWS:
            reason = f'not enough evidence yet ({len(values)} points in {len(recent)} of {AUTO_MIN_WINDOWS} windows)'
        elif hi / lo > AUTO_MAX_SPREAD:
            reason = f'points disagree too much (spread ×{hi / lo:.1f})'
        else:
            reason = None
        out[key] = dict(budget_usd=statistics.median(values), source='+'.join(x for x in AUTO if any(p['source'] == x for p in found)), readings=len(values), points=len(values),
                        by_source={x: sum(p['source'] == x for p in found) for x in AUTO if any(p['source'] == x for p in found)}, windows=len(recent), spread=[lo, hi],
                        first_date=min(p['at'] for p in found).date().isoformat(), date=max(p['at'] for p in found).date().isoformat(), used=reason is None, not_used=reason)
    return out


def auto_budgets(records, hits, snapshots, table, keep=KEEP):
    """(derive_auto(...), skipped) for the history: see auto_points."""
    points, skipped = auto_points(records, hits, snapshots, table)
    return derive_auto(points, keep), skipped


def is_auto(b):
    return b.get('source') in AUTO or b.get('source') == 'limit_hit+statusline'


def combine(manual, auto):
    """Budgets for shares: `manual` (readings and `quota set`, see derive) always win. An automatic budget is used only for a harness (and plan) the user
    has no budget of any window for, so a manual weekly budget is never mixed with an automatic 5-hour one."""
    out = dict(manual)
    for (h, m, p), v in (auto or {}).items():
        if not v.get('used', True):
            continue  # listed by `quota show`, never behind a share
        if not any(hh == h and q in (p, None) for (hh, _, q) in manual):
            out[(h, m, p)] = v
    return out


def auto_status(manual, key, v):
    """'not_used' (too little or inconsistent evidence), 'overridden' (the user has a calibration for the harness: it wins) or 'used' (behind shares)."""
    if not v.get('used', True):
        return 'not_used'
    return 'used' if combine(manual, {key: v}).get(key) is v else 'overridden'


def over_cap(auto, manual, records, table, measured=frozenset()):
    """How many turns `top` and the report would show as unknown because an automatic budget does not fit them (more than 100% of a window). Same precedence
    as the shares: a turn in `measured` (it has an observed or estimated share) and a harness with a manual budget or reading are not counted."""
    derived = combine(manual, auto)
    if not any(is_auto(v) for v in derived.values()):
        return 0
    costs = turn_costs(records, prompts.assign_prompts(records), lambda r: prompts._cost(r, table), table)
    return sum(1 for key, c in costs.items() if key not in measured and (share(derived, key[0], *c) or {}).get('unfit'))


def load_derived(path, now=None, keep=KEEP, table=None, records_fn=None):
    """derive(load(path)); {} when there is no file. A damaged file is reported on stderr by the caller; here it just yields nothing. With a price `table`,
    readings priced with another table are recomputed from the history (`records_fn`) or dropped as stale (reprice), so a turn's share never moves with the table."""
    try:
        data = load(path)
        return derive(reprice(data, table, records_fn) if table is not None else data, now, keep)
    except ValueError as exc:  # a missing file is empty and quiet; an unreadable one is empty plus a warning, as prompt_store does
        print(f'usage: warning: {exc}', file=sys.stderr)
        return {}


def public(derived):
    """The derived budgets as a sorted list of plain dicts (JSON, report)."""
    return [dict(v, harness=h, minutes=m, plan=p) for (h, m, p), v in sorted(derived.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or ''))]


def share(derived, harness, cost, lower_bound=False, plan=None):
    """The calibrated share of a turn with identified list-price `cost`, as quota_share.as_json shapes it, or None: the weekly budget when there is one,
    else the 5-hour budget. `delta_percent` is cost / budget x 100 (not rounded to whole percent here). With `lower_bound` (unpriced, incomplete or
    ambiguous requests are left out) it is only a floor. A Codex budget is found by `plan`, else the plan-less manual budget of that window; a turn spanning
    plans (MIXED) has none."""
    if cost is None:
        return None
    for minutes in (10080, 300):
        b = None if plan == MIXED else derived.get((harness, minutes, plan)) or derived.get((harness, minutes, None))
        if b:
            exact = cost / b['budget_usd'] * 100  # kept whole for the floor and the under-1% decisions; only the JSON value is rounded
            if is_auto(b) and exact > 100:  # more than a whole window cannot be: the automatic estimate does not fit this turn, so no number
                return dict(window_minutes=minutes, delta_percent=None, exact_percent=None, label='auto-calibrated', before=None, after=None, shared_with=None, lower_bound=bool(lower_bound),
                            unfit=True, calibration=dict(budget_usd=round(b['budget_usd'], 2), readings=b['readings'], spread=b['spread'] and [round(v, 2) for v in b['spread']], date=b['date'],
                                                         source=b['source'], by_source=b.get('by_source')))
            shown = math.floor(exact * 100) / 100 if lower_bound else round(exact, 2)  # a floor is never rounded up
            return dict(window_minutes=minutes, delta_percent=shown, exact_percent=exact, label='auto-calibrated' if is_auto(b) else 'calibrated', before=None, after=None, shared_with=None,
                        lower_bound=bool(lower_bound), calibration=dict(budget_usd=round(b['budget_usd'], 2), readings=b['readings'],
                                                                        spread=b['spread'] and [round(v, 2) for v in b['spread']], date=b['date'], source=b['source'], **({'by_source': b.get('by_source')} if is_auto(b) else {})))
    return None


def mark_turns(items, derived, costs):
    """Give each ranked turn without an observed share or a range (quota_share is None or unknown) a calibrated one ('auto-calibrated' for an automatic budget). `costs` is turn_costs over the whole
    history (never the filtered turn's own cost, which may be partial), keyed by (harness, session, turn_id)."""
    if not derived:
        return items
    for item in items:
        current = item.get('quota_share')
        if current and current['label'] in ('observed', 'estimate', 'range'):  # a turn with its own share or range keeps it
            continue
        cost = costs.get((item['harness'], item['session'], item['turn_id']))
        found = share(derived, item['harness'], *cost) if cost else None
        if found:
            item['quota_share'] = found
    return items


def run(args, db, records_fn, table_fn, out, auto_fn=None, cap_fn=None):
    """The `quota` command; `records_fn()` and `table_fn()` are only called by calibrate; `auto_fn(keep)` -> (automatic budgets, skipped) and `cap_fn(auto, manual)` -> turns the used ones do not fit are called by show."""
    path = path_for(db)
    now = datetime.now(timezone.utc)
    if args.quota == 'calibrate':
        reading = make_reading(records_fn(), table_fn(), args.harness, args.window, args.used, args.resets, args.at, now, args.plan)
        add_reading(path, reading)
        budget = reading['cost_usd'] / reading['used']
        out(f"stored a {args.window} {args.harness}{' ' + reading['plan'] if reading['plan'] else ''} reading: {reading['used'] * 100:g}% used, ${reading['cost_usd']:,.2f} list price seen"
            f"{' (approximate: no --resets, so the window is the last ' + args.window + ')' if reading['approximate'] else ''} -> about ${budget:,.0f} for the window.\n"
            'This is a calibration of list price, not an exact limit; see `tokenatlas quota show`.')
    elif args.quota == 'set':
        set_budget(path, args.harness, args.window, args.budget_usd, now, args.plan)
        out(f'stored a manual {args.window} {args.harness} budget of ${args.budget_usd:,.2f}')
    elif args.quota == 'forget':
        out(f'forgot {forget(path, args.harness, args.window)} entries')
    else:
        if args.keep < 1:
            raise ValueError('--keep must be at least 1')
        data = load(path)
        derived = derive(data, now, args.keep)
        auto, skipped = auto_fn(args.keep) if auto_fn else ({}, {})
        unfit = cap_fn(auto, derived) if cap_fn and auto else 0
        bad = invalid_entries(data)
        if args.json:
            out(json.dumps(dict(derived=public(derived), automatic=[dict(x, status=auto_status(derived, (x['harness'], x['minutes'], x['plan']), auto[(x['harness'], x['minutes'], x['plan'])])) for x in public(auto)], automatic_skipped=skipped, automatic_turns_over_100_percent=unfit, readings=data['readings'], budgets=data['budgets'], keep_windows=args.keep, file=str(path), invalid_entries=bad), indent=2, sort_keys=True))
        else:
            stale = sum(_valid(x) and x.get('table') != table_id(table_fn()) for x in data['readings']) if table_fn else 0
            out(render(data, derived, now, args.keep, auto, skipped, unfit) + (f'\nnote: {stale} reading{"s" * (stale != 1)} priced with another price table; `top` and the report reprice from the history' if stale else '')
                + (f'\nwarning: {bad} malformed entr{"y" if bad == 1 else "ies"} in {path} ignored' if bad else ''))


def render(data, derived, now, keep, auto=None, skipped=None, unfit=0):
    name = {300: '5-hour', 10080: 'weekly'}
    lines = []
    for (h, m, p), v in sorted(derived.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or '')):
        h = f'{h} {p}' if p else h
        if v['source'] == 'manual':
            lines.append(f"{h} {name[m]}: ${v['budget_usd']:,.0f} (manual, set {v['date']})")
        else:
            lo, hi = v['spread']
            lines.append(f"{h} {name[m]}: ≈ ${v['budget_usd']:,.0f} (median of {v['readings']} reading{'s' * (v['readings'] != 1)}, range ${lo:,.0f}-${hi:,.0f}, latest {v['date']})")
    if not lines:
        lines.append('no budget yet; `tokenatlas quota calibrate ...` or `quota set ...`')
    for x in data['readings']:
        if _valid(x):
            old = now - prompts._t(x['taken_at']) > timedelta(minutes=x['minutes'] * keep)
            lines.append(f"  reading {x['taken_at'][:16]} {x['harness']}{' ' + x['plan'] if x.get('plan') else ''} {name[x['minutes']]}: {x['used'] * 100:g}% used, ${x['cost_usd']:,.2f} seen{' (approximate)' if x.get('approximate') else ''}{' (ignored: older than ' + str(keep) + ' windows)' if old else ''}")
    if auto:
        lines.append('automatic budgets (computed from your history, not stored; manual readings and `quota set` win):')
        for (h, m, p), v in sorted(auto.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or '')):
            lo, hi = v['spread']
            lines.append(f"  {h}{' ' + p if p else ''} {name[m]}: ≈ ${v['budget_usd']:,.2f} ({v['source'].replace('_', ' ').replace('+', ' + ')}, median of {v['points']} point{'s' * (v['points'] != 1)} in {v['windows']} window{'s' * (v['windows'] != 1)}, "
                         f"range ${lo:,.2f}-${hi:,.2f}, {v['first_date']} to {v['date']}) " + {'used': 'used for shares', 'overridden': 'overridden by your calibration',
                                                                                                   'not_used': f"NOT USED: {v.get('not_used')}"}[auto_status(derived, (h, m, p), v)])
    if unfit:
        lines.append(f'{unfit} turn{"s" * (unfit != 1)} over 100% of a window with the automatic budget: shown as unknown, not as a number')
    if skipped:
        lines.append('automatic points skipped: ' + ', '.join(f'{n} {why.replace("_", " ")}' for why, n in sorted(skipped.items())))
    lines.append("List price is a proxy: the budget drifts with the model mix, and usage outside these logs (chat, other machines, cloud tasks) makes the budget too small and shares too large.")
    return '\n'.join(lines)
