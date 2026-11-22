"""Provenance labels for the numbers in JSON output (TOKEN-28).

Every numeric field of a labelled command has a default label in the registry below. A row carries a sparse
`labels` override only where its own state differs (an unknown or incomplete value). Annotation is a pure pass that only
adds `labels` keys: it never rounds, reorders or recomputes a value.

Vocabulary (README, "Number labels"):

* `logged`      recorded by the harness (token counts, counts of what was seen)
* `derived`     computed deterministically from logged values and a verified table (list-price cost, durations)
* `estimate`    model-based or inferred (quota shares, energy estimates, character-based token estimates, inferred links)
* `lower_bound` a real value that is known to be incomplete: the true figure is at least this much
* `unknown`     no value could be determined (the field is null; it is never zero)

Older fields keep their values. `provenance: computed|measured|estimate` on facts and `confidence`/`turn_confidence` on
observations map as: measured, observed, exact -> logged; computed, derived -> derived; estimate -> estimate;
lower_bound -> lower_bound; absent, null -> unknown.
"""
from __future__ import annotations

import re

SCHEMA = 'tokenatlas.labels/1'
VOCABULARY = ('logged', 'derived', 'estimate', 'lower_bound', 'unknown')
LEGACY = {'measured': 'logged', 'observed': 'logged', 'exact': 'logged', 'computed': 'derived', 'derived': 'derived',
          'estimate': 'estimate', 'lower_bound': 'lower_bound', 'absent': 'unknown', None: 'unknown'}
_WORST = ('unknown', 'lower_bound', 'estimate', 'derived', 'logged')  # when several apply, the first of these wins


def worst(*labels):
    """The least certain of the given labels (unknown > lower_bound > estimate > derived > logged)."""
    return min(labels, key=_WORST.index)


def legacy(value):
    return LEGACY[value]


# Default label per path. `[]` is any list element; `*` is any key, and as the last segment every leaf below it.
FIELDS = {
    'top': {
        'total_prompts': 'logged',
        'unattributed_observations': 'logged',
        'prompts[].cost': 'derived',
        'prompts[].credits': 'derived',
        'prompts[].duration_s': 'derived',
        'prompts[].requests': 'logged',
        'prompts[].subagent_requests': 'logged',
        'prompts[].subagents': 'logged',
        'prompts[].tokens.*': 'logged',
        'prompts[].total_tokens': 'logged',
        'prompts[].quota_share.*': 'estimate',
        'prompts[].limit_hit.*': 'logged',
        'prompts[].context.*': 'logged',
        'text_store.*': 'logged',
    },
}


def _top_row(row):
    """Overrides for one top prompt: only where the row differs from the defaults."""
    over = {}
    if row.get('cost') is None:
        over['cost'] = 'unknown'
    elif row.get('cost_complete') is False:
        over['cost'] = 'lower_bound'
    if row.get('credits') is None:
        over['credits'] = 'unknown'
    elif row.get('credits_lower_bound'):
        over['credits'] = 'lower_bound'
    if row.get('credits_lower_bound'):  # some request of the turn has incomplete token counters: the counts are a floor
        over['tokens'] = 'lower_bound'
        over['total_tokens'] = 'lower_bound'
    share = row.get('quota_share')  # one label for the whole object
    if isinstance(share, dict):
        if share.get('lower_bound'):
            over['quota_share'] = 'lower_bound'
        elif share.get('label') == 'observed':
            over['quota_share'] = 'derived'
    return over



# Commands whose numbers sit in recursive or repeated structures are labelled by regex on the normalised path.
_TOKEN_CLASSES = r'(?:fresh_input|cache_read|cache_write|output|reasoning)'
PATTERNS = {
    'session': [
        (r'^efficiency\.(?:malformed|skipped)$', 'logged'),
        (r'^records\[\]\.', 'logged'),
        (r'(?:^|[.\]])(?:tokens\.' + _TOKEN_CLASSES + r'|total|observations|priced|cost_status_counts\.[a-z_]+)$', 'logged'),
        (r'(?:^|[.\]])(?:cost_coverage|cost\.[A-Za-z]+|cost_per_pass\.[A-Za-z]+|tokens_per_pass|pass_units|units_by_outcome\.[a-z_]+)$', 'derived'),
    ],
    'report': [
        (r'^(?:totals|buckets\[\]|groups\.[a-z_]+\[\])\.(?:observations|tokens|known_tokens|ambiguous_identity_observations|ambiguous_identity_tokens'
         r'|(?:token_fields|known_token_fields|ambiguous_identity_token_fields|missing_fields)\.' + _TOKEN_CLASSES + r')$', 'logged'),
        (r'^(?:totals|buckets\[\]|groups\.[a-z_]+\[\])\.environment\..+$', 'logged'),  # provider-reported emissions; the energy proxy is a separate estimate
        (r'^source_status\..+$', 'logged'),
    ],
    'cache': [
        (r'(?:^|\.|\])(?:requests|priced_requests|unpriced_requests|write_tokens|excluded_ambiguous_requests|not_applicable_requests_with_cache_writes)$', 'logged'),
        (r'(?:cost_usd|cost_[a-z0-9]+_usd|share_of_write_cost|cache_write_share|saving_1h_vs_5m_usd)$', 'derived'),
    ],
    'overhead': [  # fixed context is measured in characters and converted at chars_per_token: everything but plain counts is an estimate
        (r'(?:^|\.)(?:n|sessions|subagent_sessions|sessions_priced|sessions_unpriced|unidentified_skill_reads|matched|excluded|sessions)$', 'logged'),
        (r'^chars_per_token$', 'estimate'),
        (r'^harnesses\..+$', 'estimate'),  # fixed_per_1k_output.value under output_lower_bound is really an upper bound; there is no such label, so it stays an estimate
    ],
    'insights': [  # the facts keep their own `provenance`; the labels follow it (see _insights_fact)
        (r'(?:^|\.|\])(?:e_in|e_out|e_cr|e_cw|factor|m_haiku|m_sonnet|m_opus|low_mwh|mid_mwh|high_mwh|mwh|tiers\.[a-z]+)$', 'estimate'),
        (r'(?:^|\.|\])(?:requests|[a-z_]*_requests|tokens|tokens\.[a-z_]+|missing\.[a-z_]+|known_tokens|known_input|observations|priced|unpriced|turns|ambiguous|incomplete|lower|unweighted|directory_count|synthetic_excluded|derived_requests|rolled_up_requests|unlinked_requests|numerator|denominator|top_n|threshold|contributor_limit|context_threshold|schema_version|big_turn)$', 'logged'),
        (r'.+', 'derived'),  # deliberate default: any other insights number is computed from logged inputs
    ],
}
_PATTERNS = {c: [(re.compile(rx), label) for rx, label in rules] for c, rules in PATTERNS.items()}


def _session_node(node):
    """A session tree node, per-model block or aggregate: unknown or incomplete cost, and an inferred link."""
    over = {}
    if 'cost' in node:
        if not node['cost']:
            over['cost'] = 'unknown'
        elif node.get('lower_bound') or (node.get('cost_coverage') is not None and node['cost_coverage'] < 1):
            over['cost'] = 'lower_bound'  # incomplete counters or unpriced requests: the cost is a floor
    if node.get('lower_bound'):
        over['tokens'] = 'lower_bound'
        over['total'] = 'lower_bound'
    link = node.get('link')
    if link == 'inferred':
        over['link'] = 'estimate'  # the child was attached by inference, not by an explicit parent reference
    elif link in ('explicit', 'root'):
        over['link'] = 'logged'
    return over


def _aggregate(node):
    """A report aggregate (total, bucket or group): a null total is unknown; counters that are not complete are a floor.

    A null per-class counter (for example `reasoning` on Claude) is `unknown` by the same rule and is not repeated on every row."""
    over = {}
    for key in ('tokens', 'known_tokens'):
        if key in node:
            if node[key] is None:
                over[key] = 'unknown'
            elif node.get('complete') is False:
                over[key] = 'lower_bound'
    return over


def _insights_fact(node):
    """A fact keeps its legacy `provenance`; its values are labelled from it (lower_bound wins)."""
    over = {}
    values = node.get('values')
    if isinstance(values, dict):
        legacy_label = LEGACY.get(node.get('provenance'), 'derived')
        if legacy_label != 'derived':
            over['values'] = legacy_label
        if values.get('lower_bound'):
            over['values'] = 'lower_bound'
    return over


def _insights_project(node):
    if node.get('cost') is None:
        return {'cost': 'unknown'}
    return {} if node.get('cost_complete') else {'cost': 'lower_bound'}


def _cache_part(node):
    """A cache cause or the TTL what-if: costs sum priced requests only."""
    if not node.get('unpriced_requests'):
        return {}
    return {k: 'lower_bound' for k in node if k.startswith('cost') and k.endswith('usd')}


def _cache_root(node):
    """Cache costs sum priced requests only: with unpriced requests they are a floor."""
    if (node.get('applicable') or {}).get('unpriced_requests'):
        return {k: 'lower_bound' for k in ('total_cost_usd', 'cache_write_cost_usd', 'recache_cost_usd')}
    return {}


NODE_RULES = {
    'insights': [(lambda n: 'provenance' in n and 'values' in n, _insights_fact), (lambda n: 'cost_complete' in n and 'directory_count' in n, _insights_project)],
    'cache': [(lambda n: 'provenance' in n and 'applicable' in n, _cache_root),
              (lambda n: 'cause' in n or 'cost_1h_usd' in n, _cache_part)],
    'top': [(lambda n: 'cost_complete' in n, _top_row)],
    'session': [(lambda n: 'cost_coverage' in n or 'link' in n, _session_node)],
    'report': [(lambda n: 'token_fields' in n, _aggregate)],
}


def registered(command):
    return command in FIELDS or command in PATTERNS


def _pattern(path):
    body = re.escape(path)
    if body.endswith(r'\.\*'):  # a trailing `.*` covers every leaf below, at any depth
        body = body[:-len(r'\.\*')] + r'(?:\.[^.\[\]]+|\[\])+'
    return re.compile('^' + body.replace(r'\*', r'[^.\[\]]+') + '$')


_COMPILED = {c: [(_pattern(p), p) for p in fields] for c, fields in FIELDS.items()}


def lookup(command, path):
    """The registry entry covering a normalised path, or None."""
    for pattern, raw in _COMPILED.get(command, ()):
        if pattern.match(path):
            return FIELDS[command][raw]
    for pattern, label in _PATTERNS.get(command, ()):
        if pattern.search(path):
            return label
    return None


def leaf_paths(data, prefix=''):
    """Normalised paths of every numeric leaf (bool excluded) of a result; list indexes become `[]`."""
    if isinstance(data, bool):
        return
    if isinstance(data, (int, float)):
        yield prefix
    elif isinstance(data, dict):
        for key, value in data.items():
            if key in ('labels', 'schema'):
                continue
            yield from leaf_paths(value, f'{prefix}.{key}' if prefix else str(key))
    elif isinstance(data, list):
        for value in data:
            yield from leaf_paths(value, prefix + '[]')


def _walk(command, node):
    if isinstance(node, list):
        return [_walk(command, v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _walk(command, v) for k, v in node.items()}  # a copy: shared, cached results are never mutated
    for predicate, rule in NODE_RULES.get(command, ()):
        if predicate(node):
            over = rule(node)
            if over:
                out['labels'] = {**node.get('labels', {}), **over} if isinstance(node.get('labels'), dict) else over
    return out


def annotate(command, data):
    """A copy of `data` with a top-level `labels` map and sparse per-node `labels` overrides.

    Results that are not a labelled analytics payload (errors, receipts) are returned unchanged."""
    if not registered(command) or not isinstance(data, dict) or data.get('ok') is False:
        return data
    if not any(lookup(command, p) for p in leaf_paths(data)):
        return data
    out = _walk(command, data)
    root = out.pop('labels', None)  # overrides for the result's own top-level fields
    fields = dict(sorted(FIELDS.get(command, {}).items()))
    out['labels'] = {'schema': SCHEMA, 'vocabulary': list(VOCABULARY), 'fields': fields}
    if root:
        out['labels']['overrides'] = root
    if command in PATTERNS:  # Python regexes applied with re.search to the normalised path, in order: the first match wins
        out['labels']['patterns'] = [[rx, label] for rx, label in PATTERNS[command]]
    return out


def strip(data):
    """Remove every `labels` key added by annotate (for the no-number-change check)."""
    if isinstance(data, dict):
        return {k: strip(v) for k, v in data.items() if k != 'labels'}
    if isinstance(data, list):
        return [strip(v) for v in data]
    return data
