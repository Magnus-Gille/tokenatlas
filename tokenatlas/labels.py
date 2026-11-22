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


ROW_RULES = {('top', 'prompts'): _top_row}


def registered(command):
    return command in FIELDS


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


def annotate(command, data):
    """A copy of `data` with a top-level `labels` map and sparse per-row `labels` overrides.

    Results that are not a labelled analytics payload (errors, receipts) are returned unchanged. Rows are shallow-copied:
    shared, cached objects are never mutated."""
    if not registered(command) or not isinstance(data, dict) or data.get('ok') is False:
        return data
    if not any(lookup(command, p) for p in leaf_paths(data)):
        return data
    out = dict(data)
    for (cmd, key), rule in ROW_RULES.items():
        rows = data.get(key) if cmd == command else None
        if isinstance(rows, list):
            labelled = []
            for row in rows:
                over = rule(row) if isinstance(row, dict) else {}
                labelled.append(dict(row, labels=over) if over else row)
            out[key] = labelled
    out['labels'] = {'schema': SCHEMA, 'vocabulary': list(VOCABULARY), 'fields': dict(sorted(FIELDS[command].items()))}
    return out


def strip(data):
    """Remove every `labels` key added by annotate (for the no-number-change check)."""
    if isinstance(data, dict):
        return {k: strip(v) for k, v in data.items() if k != 'labels'}
    if isinstance(data, list):
        return [strip(v) for v in data]
    return data
