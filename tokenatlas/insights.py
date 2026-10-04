"""Cost facts: deterministic, rule-based statements computed from the observations and the price table; no model, no interpretation.

Every fact is {id, title_key, values, computation, assumptions, provenance, ...}: `values` are the numbers, `computation` and `assumptions`
say how they were computed (English text from report_i18n.json 'en', the keys are kept for the page's own languages), and `provenance`
is 'measured' (counted from the logs) or 'computed' (arithmetic on measured values with the price table). A fact that cannot be computed
reliably, or has no data, is omitted. Costs are list prices in USD (see pricing); requests without a complete USD price are counted, never guessed.
"""
import functools
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path

from tokenatlas import credits as credit_rates, energy, limits, pricing, prompts, quota_share

I18N = Path(__file__).with_name('report_i18n.json')
BIG_TURN = 50.0
TOP_MODELS, ALTERNATIVES, MIN_SHARE = 5, 3, 0.10
PUBLIC_HARNESS = frozenset(('claude', 'codex', 'pi', 'opencode'))  # the only harness names an aggregate fact may carry; an imported snapshot can name any
PARTS = ('input', 'cache_write', 'cache_read', 'output')
PREMIUM = ('speed=fast', 'service_tier=fast', 'service_tier=priority')  # flex is a discount, not a premium
COMMON = ('ins_a_list', 'ins_a_scope')
# pricing.py's own assumption texts, mapped to the page's i18n keys (an unknown text is shown as it is)
PRICE_ASSUMPTIONS = {pricing._STANDARD_CLAUDE: 'ins_pa_speed', pricing._STANDARD_OTHER: 'ins_pa_tier', credit_rates.ASSUMED_STANDARD: 'ins_pa_credit_speed', credit_rates.FAST: 'ins_pa_credit_fast'}


@functools.lru_cache(maxsize=None)
def _texts():
    return json.loads(I18N.read_text(encoding='utf-8'))['en']


def _num(x):
    """A count or amount in a text: thousands separated, a whole float without decimals (as the page's own number format)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    return f'{int(x):,}' if float(x).is_integer() else f'{x:,.2f}'.rstrip('0')


def say(key, params):
    return re.sub(r'\{(\w+)\}', lambda m: _num(params[m[1]]), _texts()[key])


def _fact(id, values, computation, assumptions, provenance='computed', used=(), **params):
    """`used`: the (record, result, cost, tier) rows the numbers come from; _finish adds their lower-bound flag and pricing assumptions."""
    return {'id': id, 'title_key': f'ins_{id}', 'values': values, 'params': params, 'provenance': provenance,
            'computation_key': computation, 'assumption_keys': list(assumptions), '_used': list(used)}


def _finish(f, ctx):
    """Add the reliability disclosures every fact carries: requests left out (ambiguous identity) and lower-bound requests (incomplete counters),
    both as the report classifies them, the pricing assumptions with the number of requests each applies to, and the rendered English texts."""
    used = f.pop('_used')
    lower = sum(1 for x in used if not x[0].get('complete', True))
    assumed = {}
    if f['provenance'] == 'computed':
        for x in used:
            for text in x[1]['assumptions']:
                assumed[text] = assumed.get(text, 0) + 1
    f['values'].update(lower_bound=lower > 0, lower_bound_requests=lower, ambiguous_requests=ctx['ambiguous'], incomplete_requests=ctx['incomplete'])
    f['price_assumptions'] = [dict(key=PRICE_ASSUMPTIONS.get(t), text=t, requests=n) for t, n in sorted(assumed.items())]
    keys = [('ins_a_list' if ctx['retrieved'] else 'ins_a_list_nodate') if k == 'ins_a_list' else k for k in f['assumption_keys']] + ['ins_a_excluded']
    if lower:
        keys.append('ins_a_lower')
    f['assumption_keys'] = keys
    f['params'] = dict(f['params'], retrieved=ctx['retrieved'] or '', ambiguous=ctx['ambiguous'], incomplete=ctx['incomplete'], lower=lower)
    f['computation'] = say(f['computation_key'], f['params'])
    f['assumptions'] = [say(k, f['params']) for k in keys] + [say(a['key'], dict(n=a['requests'])) if a['key'] else f"{a['text']} (requests: {a['requests']:,})" for a in f['price_assumptions']]
    return f


def _limit_hits(hits, start, end):
    """Count of limit hits in the window, by limit (named by its window length); a fact the logs state, so 'measured'."""
    counts = {}
    for h in hits or ():
        at = prompts._t(h['at'])
        if (start is None or at >= start) and (end is None or at < end):
            key = (limits.label(h['window_minutes'], limits.public_reached(h['reached'])) or 'unknown', h['harness'] if h['harness'] in PUBLIC_HARNESS else 'other')
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return []
    by = [dict(limit=k[0], harness=k[1], count=n) for k, n in sorted(counts.items())]
    return [_fact('limit_hits', dict(count=sum(counts.values()), limits=by), 'ins_limit_hits_c', ('ins_a_limit_logs',), 'measured')]


def _quota_share(records, inside, memo, shares):
    """The three costliest turns with a quota snapshot in the window (turn cost as in _turns) and their combined observed or estimated share of
    their weekly limit window. A turn without a known weekly share is left out; none known, no fact. 'estimate' when any share is an estimate."""
    if not shares:
        return []
    if 'assigned' not in memo:
        memo['assigned'] = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}
    costs = {}
    for r in inside:
        found = memo['assigned'].get(id(r))
        cost = memo[id(r)][2]
        if found and cost is not None and tuple(found[:3]) in shares:
            costs[tuple(found[:3])] = costs.get(tuple(found[:3]), 0.0) + cost
    top = sorted(((c, k) for k, c in costs.items() if c > 0), key=lambda x: (-x[0], x[1]))[:3]
    known = [(c, shares[k][quota_share.WEEK], k[0]) for c, k in top if quota_share.WEEK in shares[k] and shares[k][quota_share.WEEK]['label'] in ('observed', 'estimate')]
    if not known:
        return []
    estimated = sum(1 for _, x, _h in known if x['label'] == 'estimate')
    values = dict(window_minutes=quota_share.WEEK, considered=len(top), turns=[dict(cost=c, percent=quota_share.value(x), label=x['label'], harness=h if h in PUBLIC_HARNESS else 'other') for c, x, h in known],
                  observed_turns=len(known) - estimated, estimated_turns=estimated)
    return [_fact('quota_share', values, 'ins_quota_share_c', ('ins_a_quota_account', 'ins_a_quota_whole'), 'estimate' if estimated else 'computed')]


def memo_row(r, table, memo):
    """The memoized (record, result, cost or None, tier) of a request, priced once per memo; r is kept so that its id stays unique."""
    if id(r) not in memo:
        tier = {}
        res = pricing.price_observation(r, table, tier=tier)
        memo[id(r)] = (r, res, _usd(res), tier)
    return memo[id(r)]


def memo_cost(table, memo):
    """record -> list-price USD or None through `memo`, so that a caller that also builds cost facts prices every request only once."""
    return lambda r: memo_row(r, table, memo)[2]


def _usd(res):
    """Cost in USD or None: a non-USD price is never mixed into a USD sum (as in prompts._cost)."""
    c = res['cost']
    return c if c is not None and (res['currency'] == 'USD' or (res['status'] == 'free' and res['currency'] is None)) else None


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _share(part, whole):
    return part / whole if whole else None


def cost_facts(records, table, start=None, end=None, big_turn=BIG_TURN, name=None, memo=None, credit_table=None, hits=None, universe=None, quota=None):
    """{'window', 'requests', 'priced_requests', 'unpriced_requests', 'facts': [...]} over observations with start <= ts < end.
    `name(provider, model)` maps a model to its displayed name (the shared report passes its redaction; default: the model itself).
    `memo`, a dict reused across calls with the same records and table, only saves repeated price lookups; it never changes a result.
    `credit_table` (default: the packaged credits.json) is the rate card behind the ChatGPT credit-equivalent fact.
    `universe` (the whole history's records, for a filtered report) is the basis of the turn assignment, so turn facts agree with the report's cards:
    only the selected observations are summed, but a request keeps the turn the whole history gives it.
    `hits` (limits.limit_hits) adds the limit_hits fact: how many hits fall in the window, by limit; omitted when there are none.
    `quota` (quota_share.turn_shares: {turn: {window minutes: share}}) adds the quota_share fact: the weekly-limit share of each of the three costliest turns in the window; omitted when none is known."""
    memo = {} if memo is None else memo
    name = name or (lambda provider, model: model)
    start, end = _when(start), _when(end)
    # The report's own classification (template aggregate): an observation with a synthetic (ambiguous) identity is not counted in any total;
    # an incomplete one is counted and its totals are lower bounds.
    clean = [r for r in records if not r.get('id_synthetic')]
    if 'assigned' not in memo:
        # The cards assign over every observation (an ambiguous identity still carries its turn's structure); only the numbers exclude it.
        base = universe if universe is not None else records
        full = {prompts.ident(r): a for r, a in zip(base, prompts.assign_prompts(base))}
        memo['assigned'] = {id(r): full.get(prompts.ident(r)) for r in clean}
    window = [r for r in records if (start is None or prompts._t(r['ts']) >= start) and (end is None or prompts._t(r['ts']) < end)]
    inside = [r for r in window if not r.get('id_synthetic')]
    ctx = dict(ambiguous=len(window) - len(inside), incomplete=sum(1 for r in inside if not r.get('complete', True)),
               retrieved=table.get('retrieved_on'))
    rows = [memo_row(r, table, memo) for r in inside]  # one per request: (record, result, cost or None, tier)
    priced = [x for x in rows if x[2] is not None]
    total = sum(x[2] for x in priced)
    n, k = len(rows), len(priced)
    scope = dict(requests=n, priced_requests=k, unpriced_requests=n - k, unpriced_share=_share(n - k, n))
    ctx['scope'] = scope
    facts = []
    if k and total > 0:
        # canonical (provider, model): aliases and the provider alias table collapse into the price table's own entry
        groups = {}
        for r, res, cost, _ in priced:
            ref = res['price_ref']
            g = groups.setdefault((ref['provider'], ref['model']), {'cost': 0.0, 'requests': 0, 'rows': [], 'x': []})
            g['cost'] += cost
            g['requests'] += 1
            g['rows'].append(r)
            g['x'].append(memo[id(r)])
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1]['cost'], kv[0]))
        shown = [dict(name=name(p, m), cost=g['cost'], share=g['cost'] / total, priced_requests=g['requests']) for (p, m), g in ranked[:TOP_MODELS]]
        rest = ranked[TOP_MODELS:]
        other = dict(models=len(rest), cost=sum(g['cost'] for _, g in rest), share=sum(g['cost'] for _, g in rest) / total,
                     priced_requests=sum(g['requests'] for _, g in rest)) if rest else None
        facts.append(_fact('model_share', dict(models=shown, other=other, priced_cost=total, **scope), 'ins_model_share_c',
                           (*COMMON, 'ins_a_alias'), used=priced))
        facts += _comparison(ranked, total, table, name)
        parts = {p: sum(x[1]['parts'][p] for x in priced) for p in PARTS}
        facts.append(_fact('cost_parts', dict(parts=[dict(part=p, cost=parts[p], share=parts[p] / total) for p in PARTS], priced_cost=total, **scope),
                           'ins_cost_parts_c', (*COMMON, 'ins_a_reasoning'), used=priced))
    facts += _context(rows)
    if k and total > 0:
        facts += _long(priced, table, k) + _turns(clean, inside, big_turn, scope, memo) + _subagents(priced, total, k) + _tiers(priced, table, k)
    facts += _interrupted(clean, inside, scope, memo, total)
    facts += _credits(inside, credit_table or credit_rates.packaged(), name)
    facts += _energy(inside)
    facts += _limit_hits(hits, start, end)
    facts += _quota_share(clean, inside, memo, quota)
    order = ('model_share', 'price_comparison', 'cost_parts', 'context_size', 'long_context_premium', 'big_turns', 'interrupted_turns', 'subagent_share', 'premium_tiers', 'credits', 'energy', 'limit_hits', 'quota_share')
    facts = [_finish(f, ctx) for f in sorted(facts, key=lambda f: order.index(f['id']))]
    return {'window': {'start': start and start.isoformat(), 'end': end and end.isoformat()}, **{k_: scope[k_] for k_ in ('requests', 'priced_requests', 'unpriced_requests')},
            'ambiguous_requests': ctx['ambiguous'], 'incomplete_requests': ctx['incomplete'], 'price_table': {'retrieved_on': ctx['retrieved']}, 'big_turn': big_turn, 'facts': facts}


LADDER = 8


def _reprice(rows, provider, model, table):
    """{model: total cost} of the same observations priced at each other model of `provider` that can price all of them (free, non-USD and
    infeasible models are absent). Cost is linear in the token classes once the rate set is fixed, so observations are grouped by everything
    that selects the rates (harness, tariff, whether the 5m/1h cache-write split is known, which candidate long-context thresholds the input
    exceeds), their token classes are summed and each group is priced once per candidate through price_observation, with the long-context
    decision forced to the members' common outcome. `rows` must all be priced USD observations; the result equals per-observation pricing."""
    cands = [e for e in table.get('models', ()) if e['provider'] == provider and e['model'] != model and e.get('currency') == 'USD' and not e.get('free')]
    thresholds = sorted({e['long_context']['above_input_tokens'] for e in cands if e.get('long_context')})
    groups = {}
    for r in rows:
        t = r.get('tokens') or {}
        classes = {k: t.get(k) or 0 for k in ('fresh_input', 'cache_read', 'cache_write', 'output', 'reasoning')}
        total = classes['fresh_input'] + classes['cache_read'] + classes['cache_write']
        cc = (r.get('raw_usage') or {}).get('cache_creation') or {}
        five, hour = cc.get('ephemeral_5m_input_tokens'), cc.get('ephemeral_1h_input_tokens')
        split = five is not None and hour is not None and five + hour == classes['cache_write']
        key = (r.get('harness'), json.dumps(r.get('tariff'), sort_keys=True), split, classes['cache_write'] > 0, tuple(total > x for x in thresholds))
        g = groups.setdefault(key, {'rep': r, 'tokens': dict.fromkeys(classes, 0), 'five': 0, 'hour': 0})
        for k, v in classes.items():
            g['tokens'][k] += v
        if split:
            g['five'] += five
            g['hour'] += hour
    out = {}
    for alt in cands:
        threshold = (alt.get('long_context') or {}).get('above_input_tokens')
        cost = 0.0
        for key, g in groups.items():
            rep = dict(g['rep'], provider=alt['provider'], model=alt['model'], tokens=g['tokens'],
                       raw_usage={'cache_creation': {'ephemeral_5m_input_tokens': g['five'], 'ephemeral_1h_input_tokens': g['hour']}} if key[2] else {})
            crossed = key[4][thresholds.index(threshold)] if threshold is not None else False
            c = _usd(pricing.price_observation(rep, table, long_context='always' if crossed else False))
            if c is None:
                break  # this model cannot price every request: left out
            cost += c
        else:
            out[alt['model']] = cost
    return out


def _ladder(ranked_entry, total, table, name):
    (provider, model), g = ranked_entry
    others = _reprice(g['rows'], provider, model, table)
    if not others:
        return None
    entries = sorted([(c, m, False) for m, c in others.items()] + [(g['cost'], model, True)], key=lambda x: (-x[0], x[1]))
    at = next(i for i, e in enumerate(entries) if e[2])
    first = min(max(at - LADDER // 2, 0), max(len(entries) - LADDER, 0))  # more than LADDER models: the LADDER around the actual one
    return dict(_x=g['x'], name=name(provider, model), cost=g['cost'], share=g['cost'] / total, priced_requests=g['requests'], ladder_models=len(entries),
                ladder=[dict(name=name(provider, m), cost=c, actual=a) for c, m, a in entries[first:first + LADDER]])


def _comparison(ranked, total, table, name):
    out = [x for x in (_ladder(e, total, table, name) for e in ranked if e[1]['cost'] / total >= MIN_SHARE) if x]
    used = [x for m in out for x in m['_x']]
    for m in out:
        del m['_x']
    return [_fact('price_comparison', dict(models=out), 'ins_price_comparison_c', (*COMMON, 'ins_a_samecounts', 'ins_a_alternatives'), used=used)] if out else []


def _context(rows):
    by, excluded, used = {}, 0, []
    for row in rows:
        r = row[0]
        t = r.get('tokens') or {}
        known = [t.get(c) for c in ('fresh_input', 'cache_read', 'cache_write')]
        if None in known:
            excluded += 1
        else:
            by.setdefault(r['harness'] if r['harness'] in PUBLIC_HARNESS else 'other', []).append(sum(known))  # pooled: an imported name never reaches a fact
            used.append(row)
    if not by:
        return []
    harnesses = [dict(harness=h, requests=len(v), median=float(statistics.median(v)), p90=sorted(v)[math.ceil(0.9 * len(v)) - 1]) for h, v in sorted(by.items())]
    return [_fact('context_size', dict(harnesses=harnesses, excluded_requests=excluded), 'ins_context_size_c', ('ins_a_known', 'ins_a_logs'), 'measured', used=used)]


def _complete(rows):
    """A difference between two tiers' costs over lower-bound token counts has no safe bound (a rate can be unknown, or lower, at one tier
    for the unseen tokens), so the difference facts use only requests with complete token counters and state how many were left out."""
    keep = [x for x in rows if x[0].get('complete', True)]
    return keep, len(rows) - len(keep)


def _complete_note(out):
    return ('ins_a_complete_only',) if out else ()


def _long(priced, table, k):
    # with incomplete counters the tier itself is uncertain too (the true input may cross the threshold): every incomplete request of a model
    # that has a long-context tier is left out
    complete, out = _complete([x for x in priced if x[3].get('has_long')])
    longs = [x for x in complete if x[3].get('long')]
    if not longs:
        return []
    std = [_usd(pricing.price_observation(x[0], table, long_context=False)) for x in longs]
    if None in std:
        return []  # the standard-tier cost is not computable: no premium is stated
    actual, standard = sum(x[2] for x in longs), sum(std)
    return [_fact('long_context_premium', dict(requests=len(longs), actual=actual, standard=standard, premium=actual - standard, priced_requests=k,
                                               incomplete_left_out=out),
                  'ins_long_context_premium_c', (*COMMON, 'ins_a_othertiers', *_complete_note(out)), used=longs, incomplete_out=out)]


def _tiers(priced, table, k):
    prem, out = _complete([x for x in priced if x[3].get('modifier') in PREMIUM])  # the tier is recorded; only the tokens are uncertain
    if not prem:
        return []
    std = [_usd(pricing.price_observation(x[0], table, modifiers=False)) for x in prem]
    if None in std:
        return []
    actual, standard = sum(x[2] for x in prem), sum(std)
    tiers = {}
    for x in prem:
        tiers[x[3]['modifier']] = tiers.get(x[3]['modifier'], 0) + 1
    return [_fact('premium_tiers', dict(requests=len(prem), actual=actual, standard=standard, extra=actual - standard, tiers=dict(sorted(tiers.items())), priced_requests=k,
                                        incomplete_left_out=out),
                  'ins_premium_tiers_c', (*COMMON, 'ins_a_tier_recorded', 'ins_a_flex', *_complete_note(out)), used=prem, incomplete_out=out)]


def _subagents(priced, total, k):
    subs = [x for x in priced if x[0].get('thread_kind') == 'subagent']
    if not subs:
        return []
    cost = sum(x[2] for x in subs)
    return [_fact('subagent_share', dict(subagent_cost=cost, total_cost=total, share=cost / total, subagent_requests=len(subs), priced_requests=k),
                  'ins_subagent_share_c', (*COMMON, 'ins_a_subagent'), used=priced)]


def _energy(inside):
    """Mid estimate over every request in scope (priced or not: energy needs no price), summed per token class with the model multiplier, plus
    the uncertainty range and the requests counted unweighted. The rows carry no price result, so no pricing assumption is attached."""
    by, unweighted, tiers = dict.fromkeys(energy.PER_1K, 0.0), 0, {}
    for r in inside:
        mult, weighted = energy.multiplier(r.get('provider'), r.get('model'))
        for k, v in energy.parts(r.get('tokens') or {}, mult).items():
            by[k] += v
        if weighted:
            t = energy.tier(r.get('provider'), r.get('model'))
            tiers[t] = tiers.get(t, 0) + 1
        else:
            unweighted += 1
    mid = sum(by.values())
    if not inside:
        return []  # no request in scope; with requests the fact is shown even at 0 mWh, so the unweighted count is always stated (as on the page)
    low, high = energy.bounds(mid)
    e, m = energy.PER_1K, energy.TIERS
    return [_fact('energy', dict(mid_mwh=mid, low_mwh=low, high_mwh=high, parts=[dict(part=p, mwh=by[p], share=by[p] / mid if mid else 0.0) for p in ('fresh_input', 'cache_write', 'cache_read', 'output')],
                                 requests=len(inside), unweighted_requests=unweighted, tiers=dict(sorted(tiers.items()))),
                  'ins_energy_c', ('ins_a_energy_proxy', 'ins_a_energy_constants', 'ins_a_energy_range', 'ins_a_energy_unweighted', 'ins_a_energy_reasoning'),
                  used=[(r, {'assumptions': []}, None, {}) for r in inside], e_in=e['fresh_input'], e_out=e['output'], e_cr=e['cache_read'], e_cw=e['cache_write'],
                  m_haiku=m['haiku'], m_sonnet=m['sonnet'], m_opus=m['opus'], factor=energy.UNCERTAINTY, unweighted=unweighted)]


def _credits(inside, ctable, name):
    """ChatGPT credit equivalent of the OpenAI requests (credit_rates: standard-speed rate card). Requests of other providers are not part of it;
    OpenAI requests without a rate (unknown model), at another speed or tier, or with unknown token counts are counted and named, never guessed.
    Independent of the USD price table: a model can have a credit rate and no list price or the reverse."""
    by, rated, unrated, nonstd, unknown, openai, writes = {}, [], {}, {}, 0, 0, 0
    for r in inside:
        res = credit_rates.credit_observation(r, ctable)
        if res['status'] == 'other_provider':
            continue
        openai += 1
        if res['status'] == 'credited':
            g = by.setdefault(res['model'], {'credits': 0.0, 'requests': 0, 'provider': r.get('provider')})
            g['credits'] += res['credits']
            g['requests'] += 1
            rated.append((r, {'assumptions': res['assumptions']}, None, {}))
        elif res['status'] == 'nonstandard':
            nonstd[res['label']] = nonstd.get(res['label'], 0) + 1
        elif res['status'] == 'partial':
            unknown += 1
        elif res['status'] == 'cache_write':  # the card has no cache-write rate
            writes += 1
        else:  # a model without a rate
            key = name(r.get('provider'), res['model']) if res['model'] else 'unknown'
            unrated[key] = unrated.get(key, 0) + 1
    if not openai:
        return []
    total = sum(g['credits'] for g in by.values())
    ranked = sorted(by.items(), key=lambda kv: (-kv[1]['credits'], kv[0]))
    shown = [dict(name=name(g['provider'], m), credits=g['credits'], requests=g['requests']) for m, g in ranked[:TOP_MODELS]]
    rest = ranked[TOP_MODELS:]
    other = dict(models=len(rest), credits=sum(g['credits'] for _, g in rest), requests=sum(g['requests'] for _, g in rest)) if rest else None
    return [_fact('credits', dict(credits=total, credited_requests=len(rated), openai_requests=openai, models=shown, other=other,
                                  unrated=[dict(name=k, requests=v) for k, v in sorted(unrated.items())], unrated_requests=sum(unrated.values()),
                                  nonstandard=dict(sorted(nonstd.items())), nonstandard_requests=sum(nonstd.values()), unknown_token_requests=unknown, cache_write_requests=writes),
                  'ins_credits_c', ('ins_a_credit_table', 'ins_a_credit_standard', 'ins_a_credit_notdrawn', 'ins_a_credit_money', 'ins_a_credit_plans', 'ins_a_credit_scope'), used=rated,
                  credit_url=ctable['source_url'], credit_retrieved=ctable['retrieved_on'], credit_fast=ctable['fast_multiplier'])]


def _interrupted(records, inside, scope, memo, total):
    """Turns the user stopped (an observation of the turn carries the 'interrupted' flag, which the harness logged): how many, what their requests
    cost at list prices and the share of all priced cost in the window. A turn without a priced request is counted but adds no cost; it is
    never guessed. Omitted when no turn in the window was interrupted."""
    if 'assigned' not in memo:
        memo['assigned'] = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}
    turns = {}
    for r in inside:
        found = memo['assigned'][id(r)]
        if found:
            t = turns.setdefault(found[:3], [False, 0.0, 0, 0])  # interrupted, cost, priced requests, requests
            t[0] = t[0] or 'interrupted' in (r.get('flags') or ())
            t[3] += 1
            cost = memo[id(r)][2]
            if cost is not None:
                t[1] += cost
                t[2] += 1
    stopped = [t for t in turns.values() if t[0]]
    if not stopped:
        return []
    used = [memo[id(r)] for r in inside if memo[id(r)][2] is not None]  # the share's denominator is every priced request, so its disclosures are too
    cost = sum(t[1] for t in stopped)
    return [_fact('interrupted_turns', dict(count=len(stopped), cost=cost, priced_cost=total, share=_share(cost, total),
                                            unpriced_turns=sum(1 for t in stopped if not t[2]), partly_priced_turns=sum(1 for t in stopped if 0 < t[2] < t[3]), **scope),
                  'ins_interrupted_turns_c', (*COMMON, 'ins_a_turn_window', 'ins_a_turn_lower', 'ins_a_unattributed', 'ins_a_interrupted'),
                  provenance='computed', used=used)]


def _turns(records, inside, big_turn, scope, memo):
    """Turn costs as prompts.top_prompts defines them (assign_prompts over all records; a turn's cost is the sum of its priced requests inside the
    window, a turn without a priced request has none), without building its per-turn detail: that is what makes 200k observations affordable."""
    if 'assigned' not in memo:
        memo['assigned'] = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}  # the same for every window over these records
    turns, unattributed, used = {}, 0, []
    for r in inside:
        found = memo['assigned'][id(r)]
        if not found:
            unattributed += 1
            continue
        t = turns.setdefault(found[:3], [0.0, 0, 0])
        t[1] += 1
        cost = memo[id(r)][2]
        if cost is not None:
            t[0] += cost
            t[2] += 1
            used.append(memo[id(r)])
    costed = [(t[0], t[1]) for t in turns.values() if t[2]]
    big = [x for x in costed if x[0] >= big_turn]
    if not big:
        return []
    attributed, cost = sum(x[0] for x in costed), sum(x[0] for x in big)
    return [_fact('big_turns', dict(threshold=big_turn, count=len(big), turns=len(costed), cost=cost, attributed_cost=attributed, share=cost / attributed,
                                    median_requests=float(statistics.median(x[1] for x in big)), unattributed_requests=unattributed, **scope),
                  'ins_big_turns_c', (*COMMON, 'ins_a_turn_window', 'ins_a_turn_lower', 'ins_a_unattributed'), used=used, big_turn=big_turn)]


def public(result):
    """The compact form for the report payload: keys and numbers only (the page has the texts in its own languages)."""
    return {**result, 'facts': [{k: v for k, v in f.items() if k not in ('computation', 'assumptions')} for f in result['facts']]}


def _usd_text(x):
    return f'${x:,.2f}'


def _pct(x):
    return f'{100 * x:.1f}%'


def _signed(x):
    return ('+' if x >= 0 else '-') + _usd_text(abs(x))


def _prq(n):
    return f"{n:,} priced request" + ('' if n == 1 else 's')


def _rq(n):
    return f"{n:,} request" + ('' if n == 1 else 's')


def _quota_pct(percent, label):
    whole = int(percent + 0.5)
    return '< 1%' if whole < 1 else f"{'≈' if label == 'estimate' else '~'}{whole}%"


_AGENT_NAMES = {'claude': 'Claude', 'codex': 'Codex', 'pi': 'Pi', 'opencode': 'OpenCode'}  # how a limit names its agent (not the Claude Code product name)
_LIMIT_NAMES = {'five_hour': '5-hour limit', 'weekly': 'weekly limit'}


def _lines(f):
    v, i = f['values'], f['id']
    lb = '≥' if v['lower_bound'] else ''
    u = lambda x: lb + _usd_text(x)
    if i == 'model_share':
        out = [f"{m['name']}: {u(m['cost'])} ({_pct(m['share'])}), {_prq(m['priced_requests'])}" for m in v['models']]
        if v['other']:
            o = v['other']
            out.append(f"other ({o['models']} models): {u(o['cost'])} ({_pct(o['share'])}), {_prq(o['priced_requests'])}")
        return out + [f"priced cost: {u(v['priced_cost'])} over {_prq(v['priced_requests'])}",
                      f"without a complete USD list price: {v['unpriced_requests']:,} of {_rq(v['requests'])} ({_pct(v['unpriced_share'])})"]
    if i == 'price_comparison':
        out = []
        for m in v['models']:
            out.append(f"{m['name']}: {u(m['cost'])} ({_pct(m['share'])} of priced cost, {_prq(m['priced_requests'])}); the same tokens at list prices of {len(m['ladder'])} of {m['ladder_models']} models from the same provider, highest first:")
            out += [f"  {x['name']}: {u(x['cost'])}" + ('  <- model used' if x['actual'] else '') for x in m['ladder']]
        return out
    if i == 'cost_parts':
        return [f"{p['part'].replace('_', ' ')}: {u(p['cost'])} ({_pct(p['share'])})" for p in v['parts']] + [f"priced cost: {u(v['priced_cost'])}"]
    if i == 'context_size':
        out = [f"{h['harness']}: median {lb}{h['median']:,.0f} tokens, p90 {lb}{h['p90']:,} tokens, {_rq(h['requests'])}" for h in v['harnesses']]
        return out + ([f"not counted (an input class is unknown): {_rq(v['excluded_requests'])}"] if v['excluded_requests'] else [])
    if i == 'long_context_premium':
        return [f"requests at the long-context tier: {v['requests']:,} of {v['priced_requests']:,} priced", f"cost at the tier applied: {u(v['actual'])}",
                f"same requests at the standard tier: {u(v['standard'])}", f"premium: {u(v['premium'])}"]
    if i == 'big_turns':
        return [f"turns costing >= {_usd_text(v['threshold'])}: {v['count']:,} of {v['turns']:,} turns with a priced request",
                f"their cost: {u(v['cost'])} of {u(v['attributed_cost'])} ({_pct(v['share'])})", f"median requests per such turn: {_num(v['median_requests'])}"]
    if i == 'interrupted_turns':
        out = [f"turns interrupted by the user: {v['count']:,}", f"their cost: {u(v['cost'])}" + (f" ({_pct(v['share'])} of {u(v['priced_cost'])} priced cost)" if v['share'] is not None else '')]
        if v['unpriced_turns']:
            out.append(f"without a priced request (no cost counted): {v['unpriced_turns']:,}")
        if v['partly_priced_turns']:
            out.append(f"with some unpriced requests (cost is a lower bound): {v['partly_priced_turns']:,}")
        return out
    if i == 'quota_share':
        each = [f"{_quota_pct(x['percent'], x['label'])} of the weekly {_AGENT_NAMES[x['harness']]} limit" if x['harness'] in _AGENT_NAMES else f"{_quota_pct(x['percent'], x['label'])} of the weekly limit (other agent)" for x in v['turns']]
        listed = ', '.join(each[:-1]) + (' and ' if len(each) > 1 else '') + each[-1]
        return [f"your {len(v['turns'])} costliest turns used {listed} (each of its own window)",
                f"turns with a known weekly share: {len(v['turns'])} of {v['considered']} costliest"]
    if i == 'limit_hits':
        return [f"limit hits: {v['count']:,}"] + [f"{x['harness']} {_LIMIT_NAMES.get(x['limit'], x['limit'])}: {x['count']:,}" for x in v['limits']]
    if i == 'subagent_share':
        return [f"cost from subagents: {u(v['subagent_cost'])} of {u(v['total_cost'])} ({_pct(v['share'])})", f"requests from subagents: {v['subagent_requests']:,} of {v['priced_requests']:,} priced"]
    if i == 'energy':
        parts = {'fresh_input': 'input (uncached)', 'cache_write': 'cache write', 'cache_read': 'cache read', 'output': 'output'}
        return [f"mid estimate (order of magnitude, not a measurement): {lb}{energy.fmt(v['mid_mwh'])}",
                f"range (mid / {energy.UNCERTAINTY} to mid x {energy.UNCERTAINTY}): {lb}{energy.fmt(v['low_mwh'])} to {lb}{energy.fmt(v['high_mwh'])}"] + \
               [f"{parts[p['part']]}: {_pct(p['share'])} of the mid estimate" for p in v['parts']] + \
               [f"requests: {v['requests']:,}; without model weighting (counted with multiplier 1): {v['unweighted_requests']:,}"]
    if i == 'credits':
        out = [f"{m['name']}: {'≥ ' if lb else '≈ '}{credit_rates.fmt(m['credits'])} credits, {_rq(m['requests'])}" for m in v['models']]
        if v['other']:
            o = v['other']
            out.append(f"other ({o['models']} models): {'≥ ' if lb else '≈ '}{credit_rates.fmt(o['credits'])} credits, {_rq(o['requests'])}")
        out += [f"credit equivalent: {'≥ ' if lb else '≈ '}{credit_rates.fmt(v['credits'])} credits over {v['credited_requests']:,} of {_rq(v['openai_requests'])} from OpenAI"]
        if v['unrated']:
            out.append(f"left out, no credit rate for the model: {v['unrated_requests']:,} ({', '.join(x['name'] + ' ' + format(x['requests'], ',') for x in v['unrated'])})")
        if v['nonstandard']:
            out.append(f"left out, not standard speed: {v['nonstandard_requests']:,} ({', '.join(f'{k} {n:,}' for k, n in v['nonstandard'].items())})")
        if v['cache_write_requests']:
            out.append(f"left out, requests with cache writes (no credit rate): {v['cache_write_requests']:,}")
        if v['unknown_token_requests']:
            out.append(f"left out, unknown token counts: {v['unknown_token_requests']:,}")
        return out
    return [f"requests at a fast or priority tier: {v['requests']:,} ({', '.join(f'{k} {n:,}' for k, n in v['tiers'].items())})", f"cost at the tier applied: {u(v['actual'])}",
            f"same requests at the standard tier: {u(v['standard'])}", f"extra cost: {u(v['extra'])}"]


def render_text(result):
    """Human-readable English block per fact: the numbers, then the computation and assumptions on indented lines. Aggregates only."""
    w = result['window']
    span = f"{w['start'] or 'the first request'} to {w['end'] or 'now'}" if w['start'] or w['end'] else 'all history'
    out = ['Cost facts: list-price USD and an energy estimate, computed locally from saved observations (no language model, no interpretation)',
           f"Window: {span}", f"Requests: {result['requests']:,} ({result['priced_requests']:,} with a complete USD list price, {result['unpriced_requests']:,} without); left out as ambiguous: {result['ambiguous_requests']:,}; incomplete (lower bounds): {result['incomplete_requests']:,}"]
    if not result['facts']:
        out.append('No cost facts can be computed for this window.')
    for n, f in enumerate(result['facts'], 1):
        out += ['', f"{n}. {_texts()[f['title_key']]} [{f['provenance']}]"] + [f'   {x}' for x in _lines(f)]
        out += [f"   computation: {f['computation']}", '   assumptions:'] + [f'     - {a}' for a in f['assumptions']]
    return '\n'.join(out)
