"""Explain cache spend (TOKEN-27): which cache writes were full re-caches, why, what they cost, and what a different cache TTL would have cost.

Everything is computed from the saved observations and the price table: no model, no interpretation. Costs are list-price USD
estimates; a request without a complete price is counted, never guessed. Claude requests carry the 5m/1h cache-write split;
other harnesses are reported as not applicable (their logs have no comparable TTL).

Heuristic (documented in the README):
  * A *full re-cache* is a request whose cache write is at least `min_write` tokens and at least `share` of its input
    (fresh + cache read + cache write): the prefix was rewritten instead of read.
  * Its cause, first match wins: cold_start (first request of the thread), idle_over_1h (more than an hour since the thread's
    previous request: every TTL had expired), idle_5m_to_1h (more than 5 minutes, and the previous request wrote only
    5-minute cache), model_switch, effort_switch, speed_switch, other (compaction, edited prefix, ...). A pause beats a switch
    because a switch usually follows a pause and the cache was gone either way.
  * A thread is a session plus agent, so subagents are separate prefixes. The gap is between the recorded times of
    consecutive requests of the thread (the time a request finished), so it is approximate by one response time.
"""
from collections import defaultdict

from tokenatlas import insights, pricing, prompts

SCHEMA = 1
MIN_WRITE, SHARE = 5000, 0.5
FIVE, HOUR = 300, 3600
CAUSES = ('cold_start', 'idle_over_1h', 'idle_5m_to_1h', 'model_switch', 'effort_switch', 'speed_switch', 'other')
IDLE = ('idle_over_1h', 'idle_5m_to_1h')


def _int(value):
    return value if isinstance(value, int) and value > 0 else 0


def _hour_write(record):
    return _int(((record.get('raw_usage') or {}).get('cache_creation') or {}).get('ephemeral_1h_input_tokens'))


def _speed(record):
    return (record.get('tariff') or {}).get('speed')


def classify(record, previous, gap, min_write=MIN_WRITE, share=SHARE):
    """The cause of a full re-cache, None when the request is not one. `previous` is the thread's previous request or None."""
    tokens = record['tokens']
    write = _int(tokens.get('cache_write'))
    total = _int(tokens.get('fresh_input')) + _int(tokens.get('cache_read')) + write
    if write < min_write or write < share * total:
        return None
    if previous is None:
        return 'cold_start'
    if gap > HOUR:
        return 'idle_over_1h'
    if gap > FIVE and not _hour_write(previous):
        return 'idle_5m_to_1h'
    if record.get('model') != previous.get('model'):
        return 'model_switch'
    if record.get('effort') and previous.get('effort') and record['effort'] != previous['effort']:
        return 'effort_switch'
    if _speed(record) != _speed(previous) and (_speed(record) or _speed(previous)):
        return 'speed_switch'
    return 'other'


def _threads(records):
    threads = defaultdict(list)
    for r in records:
        threads[(r['harness'], r['session'], r.get('agent') or 'main')].append((prompts._t(r['ts']), r))
    for rows in threads.values():
        rows.sort(key=lambda x: x[0])
    return threads


def _what_if(rows, table):
    """(costs by TTL, priced, unpriced) replaying the thread's gaps under a 5-minute and a 1-hour cache-write TTL.

    The cached context of a request is its cache read plus its cache write. Within the TTL the previous request's context is read
    and only the growth is written; after the TTL, or when the model changed, the whole context is written again."""
    costs, priced, unpriced = {FIVE: 0.0, HOUR: 0.0}, 0, 0
    previous = None
    for when, r in rows:
        context = _int(r['tokens'].get('cache_read')) + _int(r['tokens'].get('cache_write'))
        rates = pricing.unit_prices(r, table)
        if rates is None:
            unpriced += 1
        else:
            priced += 1
            _, write5, write1, read, _ = rates
            for ttl, write in ((FIVE, write5), (HOUR, write1)):
                if previous is None or r.get('model') != previous[1].get('model') or (when - previous[0]).total_seconds() > ttl:
                    written, reread = context, 0
                else:
                    reread = min(previous[2], context)
                    written = context - reread
                costs[ttl] += written * write + reread * read
        previous = (when, r, context)
    return costs, priced, unpriced


def analyze(records, table, start=None, end=None, min_write=MIN_WRITE, share=SHARE, top=10, memo=None, private=False):
    """The `tokenatlas cache` result for the requests in [start, end)."""
    memo = {} if memo is None else memo
    inside = [r for r in records if (start is None or prompts._t(r['ts']) >= start) and (end is None or prompts._t(r['ts']) < end)]
    claude = [r for r in inside if r['harness'] == 'claude']
    other_writes = sum(1 for r in inside if r['harness'] != 'claude' and _int(r['tokens'].get('cache_write')))
    causes = {c: dict(requests=0, write_tokens=0, cost_usd=0.0, unpriced_requests=0) for c in CAUSES}
    by_model = defaultdict(lambda: defaultdict(float))
    sessions = defaultdict(lambda: dict(recaches=0, write_tokens=0, cost_usd=0.0, unpriced=0, longest_gap=0.0))
    whatif = {FIVE: 0.0, HOUR: 0.0}
    whatif_priced = whatif_unpriced = 0
    total_cost = write_cost = 0.0
    priced_requests = unpriced_requests = 0
    for (harness, session, agent), rows in _threads(claude).items():
        costs, n_priced, n_unpriced = _what_if(rows, table)
        for ttl in whatif:
            whatif[ttl] += costs[ttl]
        whatif_priced += n_priced
        whatif_unpriced += n_unpriced
        previous = previous_when = None
        for when, r in rows:
            _, res, cost, _tier = insights.memo_row(r, table, memo)
            part = res['parts'].get('cache_write')
            if cost is None:
                unpriced_requests += 1
            else:
                priced_requests += 1
                total_cost += cost
                write_cost += part or 0.0
            gap = (when - previous_when).total_seconds() if previous_when else 0.0
            cause = classify(r, previous, gap, min_write, share)
            if cause:
                entry = causes[cause]
                entry['requests'] += 1
                entry['write_tokens'] += _int(r['tokens'].get('cache_write'))
                if part is None:
                    entry['unpriced_requests'] += 1
                else:
                    entry['cost_usd'] += part
                    by_model[(harness, r.get('model') or 'unknown')][cause] += part
                if cause in IDLE:
                    s = sessions[(harness, session)]
                    s['recaches'] += 1
                    s['write_tokens'] += _int(r['tokens'].get('cache_write'))
                    s['longest_gap'] = max(s['longest_gap'], gap)
                    if part is None:
                        s['unpriced'] += 1
                    else:
                        s['cost_usd'] += part
                    s.setdefault('project', r.get('project_label'))
            previous, previous_when = r, when
    ranked = sorted(sessions.items(), key=lambda kv: (-kv[1]['cost_usd'], -kv[1]['write_tokens'], kv[0]))[:top]
    recached = sum(c['cost_usd'] for c in causes.values())
    saving = whatif[FIVE] - whatif[HOUR]
    return {
        'schema': SCHEMA,
        'provenance': 'computed',
        'assumptions': ['List-price estimates from the price table, not billed amounts.',
                        f'A full re-cache is a cache write of at least {min_write:,} tokens that is at least {share:.0%} of the request input.',
                        'Cause precedence: cold start, idle over 1 hour, idle over 5 minutes after a 5-minute write, model, effort, speed switch, other.',
                        'A thread is a session plus agent; gaps are between the recorded times of consecutive requests.'],
        'window': {'start': start.isoformat() if start else None, 'end': end.isoformat() if end else None},
        'applicable': {'harness': 'claude', 'requests': len(claude), 'priced_requests': priced_requests, 'unpriced_requests': unpriced_requests,
                       'not_applicable_requests_with_cache_writes': other_writes},
        'cache_write_cost_usd': write_cost, 'total_cost_usd': total_cost,
        'cache_write_share': (write_cost / total_cost) if total_cost else None,
        'recache_cost_usd': recached,
        'causes': [dict(cause=c, share_of_write_cost=(v['cost_usd'] / write_cost) if write_cost else None, **v) for c, v in causes.items()],
        'by_model': [dict(harness=h, model=m, cost_usd=sum(v.values()), causes=dict(v)) for (h, m), v in sorted(by_model.items(), key=lambda kv: -sum(kv[1].values()))],
        'resume_tax': [dict(harness=h, session=str(s)[:8], recaches=v['recaches'], write_tokens=v['write_tokens'], cost_usd=v['cost_usd'],
                            unpriced_requests=v['unpriced'], longest_gap_hours=round(v['longest_gap'] / 3600, 1),
                            **({'project': v.get('project')} if private else {})) for (h, s), v in ranked],
        'ttl_what_if': {'priced_requests': whatif_priced, 'unpriced_requests': whatif_unpriced,
                        'cost_5m_usd': whatif[FIVE] if whatif_priced else None, 'cost_1h_usd': whatif[HOUR] if whatif_priced else None,
                        'saving_1h_vs_5m_usd': saving if whatif_priced else None,
                        'cheaper': None if not whatif_priced else ('1h' if saving > 1e-9 else '5m' if saving < -1e-9 else 'equal'),
                        'note': 'Cache read and write cost of the same requests replayed under each TTL; output and fresh input are the same either way.'},
    }


def _usd(x):
    return 'n/a' if x is None else f'${x:,.2f}'


def render_text(result):
    a, w = result['applicable'], result['ttl_what_if']
    lines = ['Cache spend (list-price estimates, Claude requests)']
    if not a['requests']:
        return lines[0] + '\n  No Claude requests in the window.'
    share = result['cache_write_share']
    lines.append(f"  Cache writes cost {_usd(result['cache_write_cost_usd'])}" + (f' ({share:.0%} of {_usd(result["total_cost_usd"])})' if share is not None else '')
                 + f"; full re-caches cost {_usd(result['recache_cost_usd'])}.")
    if a['unpriced_requests']:
        lines.append(f"  {a['unpriced_requests']:,} requests have no complete price and are left out of every cost.")
    lines.append('  Full re-caches by cause:')
    for c in result['causes']:
        if c['requests']:
            part = '' if c['share_of_write_cost'] is None else f"  {c['share_of_write_cost']:.0%} of write cost"
            lines.append(f"    {c['cause']:<14} {c['requests']:>6,} requests  {c['write_tokens']:>14,} tokens  {_usd(c['cost_usd']):>10}{part}")
    if result['resume_tax']:
        lines.append('  Resume tax (re-caches after an idle gap), most expensive sessions:')
        for s in result['resume_tax']:
            lines.append(f"    {s['harness']}:{s['session']}  {s['recaches']} re-caches  {_usd(s['cost_usd'])}  longest pause {s['longest_gap_hours']} h"
                         + (f"  {s['project']}" if 'project' in s else ''))
    if w['cheaper']:
        lines.append(f"  TTL what-if (cache read + write of the same requests): 5-minute {_usd(w['cost_5m_usd'])}, 1-hour {_usd(w['cost_1h_usd'])}; "
                     f"{'the 1-hour cache would have saved ' + _usd(w['saving_1h_vs_5m_usd']) if w['cheaper'] == '1h' else 'the 5-minute cache would have saved ' + _usd(-w['saving_1h_vs_5m_usd']) if w['cheaper'] == '5m' else 'no difference'}.")
    if a['not_applicable_requests_with_cache_writes']:
        lines.append(f"  Not applicable: {a['not_applicable_requests_with_cache_writes']:,} requests from other harnesses wrote cache; their logs have no comparable TTL.")
    lines.append('  What to do: start fresh or /compact before resuming after a long break, and avoid switching model mid-session.')
    return '\n'.join(lines)
