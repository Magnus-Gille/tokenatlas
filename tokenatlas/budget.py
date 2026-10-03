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
    unknown plan, is left out and makes the turn a lower bound; a turn spanning plans has plan MIXED, and so does one on a plan with several detected
    accounts (multi_counter_plans): no budget fits it. Claude has no plan (None)."""
    out = {}
    multi = multi_counter_plans(records) if any(r['harness'] in PLANNED and plan_of(r) for r in records) else frozenset()
    canon = _canon(table)
    for record, a in zip(records, assigned):
        if not a or record['harness'] not in PROVIDER or not _provider_ok(record, record['harness'], canon):
            continue
        key = tuple(a[:3])
        by_plan, lower = out.setdefault(key, [{}, False])
        plan = plan_of(record) if record['harness'] in PLANNED else None
        if record.get('id_synthetic') or (record['harness'] in PLANNED and plan is None):
            out[key][1] = True
            continue
        c = cost_of(record)
        if c is None or not record.get('complete', True):
            out[key][1] = True
        by_plan[plan] = by_plan.get(plan, 0.0) + (c or 0.0)
    result = {}
    for key, (by_plan, lower) in out.items():
        if len(by_plan) > 1 or any(p in multi for p in by_plan):
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


def cost_seen(records, table, harness, start, end, plan=None):
    """(list-price USD, unpriced requests, excluded requests) of one harness's requests on its subscription provider in [start, end]; ambiguous (id_synthetic)
    requests are left out. For Codex only requests of `plan` count (a request on another plan is another account's usage and is ignored); a request without
    a quota snapshot has an unknown plan and is left out and counted as excluded."""
    total, unpriced, excluded = 0.0, 0, 0
    canon = _canon(table)
    for r in records:
        if not _provider_ok(r, harness, canon) or r.get('id_synthetic'):
            continue
        t = prompts._t(r['ts'])
        if not start <= t <= end:
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
    return [dict(harness=h, minutes=m, plan=p, **v) for (h, m, p), v in sorted(derived.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or ''))]


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
            shown = math.floor(exact * 100) / 100 if lower_bound else round(exact, 2)  # a floor is never rounded up
            return dict(window_minutes=minutes, delta_percent=shown, exact_percent=exact, label='calibrated', before=None, after=None, shared_with=None,
                        lower_bound=bool(lower_bound), calibration=dict(budget_usd=round(b['budget_usd'], 2), readings=b['readings'],
                                                                        spread=b['spread'] and [round(v, 2) for v in b['spread']], date=b['date'], source=b['source']))
    return None


def mark_turns(items, derived, costs):
    """Give each ranked turn without an observed or estimated share (quota_share is None or unknown) a calibrated one. `costs` is turn_costs over the whole
    history (never the filtered turn's own cost, which may be partial), keyed by (harness, session, turn_id)."""
    if not derived:
        return items
    for item in items:
        current = item.get('quota_share')
        if current and current['label'] in ('observed', 'estimate'):
            continue
        cost = costs.get((item['harness'], item['session'], item['turn_id']))
        found = share(derived, item['harness'], *cost) if cost else None
        if found:
            item['quota_share'] = found
    return items


def run(args, db, records_fn, table_fn, out):
    """The `quota` command; `records_fn()` and `table_fn()` are only called by calibrate."""
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
        bad = invalid_entries(data)
        if args.json:
            out(json.dumps(dict(derived=public(derived), readings=data['readings'], budgets=data['budgets'], keep_windows=args.keep, file=str(path), invalid_entries=bad), indent=2, sort_keys=True))
        else:
            stale = sum(_valid(x) and x.get('table') != table_id(table_fn()) for x in data['readings']) if table_fn else 0
            out(render(data, derived, now, args.keep) + (f'\nnote: {stale} reading{"s" * (stale != 1)} priced with another price table; `top` and the report reprice from the history' if stale else '')
                + (f'\nwarning: {bad} malformed entr{"y" if bad == 1 else "ies"} in {path} ignored' if bad else ''))


def render(data, derived, now, keep):
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
    lines.append("List price is a proxy: the budget drifts with the model mix, and usage outside these logs (chat, other machines, cloud tasks) makes the budget too small and shares too large.")
    return '\n'.join(lines)
