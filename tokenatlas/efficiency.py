"""Bounded, privacy-safe token efficiency facts for offline reports.

This module only consumes the allowlisted rows passed by its caller. It never
reads the clock, source files, or any raw usage/session metadata.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tokenatlas import prompts

_INPUT = ('fresh_input', 'cache_read', 'cache_write')
_TOKENS = (*_INPUT, 'output')
_PUBLIC_PROJECT = re.compile(r'^Project ([0-9]{3,})$')
_REPORT_PROJECT = re.compile(r'^\x01p([0-9]{3,})$')
_FILTER_FLAGS = frozenset(('harness', 'provider', 'model', 'project_id', 'thread_kind',
                           'effort', 'session', 'agent', 'search', 'zoom'))


def _instant(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name} must be an offset ISO timestamp')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be an offset ISO timestamp') from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f'{name} must include a UTC offset')
    return dt.astimezone(timezone.utc).replace(microsecond=(dt.microsecond//1000)*1000)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _setting_int(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def _safe_filters(filters):
    """Keep only fixed filter-presence flags; filter values may contain identities."""
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError('filters must be a mapping or None')
    safe = {}
    for key, value in filters.items():
        if key in _FILTER_FLAGS and isinstance(value, bool):
            safe[key] = value
    return safe


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _clean_row(row):
    if not isinstance(row, dict):
        raise ValueError('each row must be a mapping')
    ts = _instant(row.get('ts'), 'row ts')
    raw_tokens = row.get('tokens')
    raw_tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    tokens = {key: _count(raw_tokens.get(key)) for key in (*_TOKENS, 'reasoning')}
    prompt = row.get('prompt')
    if prompt is not None and (isinstance(prompt, bool) or not isinstance(prompt, int) or prompt < 0):
        raise ValueError('prompt must be a nonnegative integer or None')
    project = row.get('efficiency_project_id', row.get('project_id'))
    # Project identifiers are report aliases, never caller-supplied names.
    match = _REPORT_PROJECT.fullmatch(project) if isinstance(project, str) else None
    if match:
        project = f'Project {match.group(1)}'
    else:
        match = _PUBLIC_PROJECT.fullmatch(project) if isinstance(project, str) else None
        project = f'Project {match.group(1)}' if match else None
    return {
        'ts': ts, 'tokens': tokens, 'prompt': prompt, 'project_id': project,
        'complete': row.get('complete') is True,
        'id_synthetic': row.get('id_synthetic') is True,
        'thread_kind': row.get('thread_kind') if row.get('thread_kind') in ('main', 'subagent', 'automation') else 'other',
        'turn_confidence': row.get('turn_confidence') if row.get('turn_confidence') in ('observed', 'derived', 'absent') else 'absent',
        'auto': row.get('efficiency_auto_review') is True,
        'rolled': row.get('efficiency_rolled_up') is True,
    }


def _summary(rows, synthetic_excluded=0):
    missing = {key: sum(r['tokens'][key] is None for r in rows) for key in _TOKENS}
    tokens = {key: sum(r['tokens'][key] or 0 for r in rows) for key in _TOKENS}
    known_tokens = sum(tokens.values())
    known_input = sum(tokens[key] for key in _INPUT)
    incomplete = any(not r['complete'] or any(r['tokens'][key] is None for key in _TOKENS) for r in rows)
    return {
        'requests': len(rows), 'tokens': tokens, 'missing': missing,
        'known_tokens': known_tokens, 'known_input': known_input,
        'complete': bool(rows) and not incomplete and not synthetic_excluded,
    }


def _known_token(row):
    return sum(row['tokens'][key] or 0 for key in _TOKENS)


def _known_input(row):
    return sum(row['tokens'][key] or 0 for key in _INPUT)


def _is_complete_input(row):
    return all(row['tokens'][key] is not None for key in _INPUT)


def _turn_rows(rows, predicate=lambda r: True):
    groups = {}
    for row in rows:
        if row['prompt'] is not None and predicate(row):
            groups.setdefault(row['prompt'], []).append(row)
    return groups


def _turn_summary(turn, rows):
    return {'turn': turn, **_summary(rows)}


def _fact(fid, formula, numerator, denominator, complete, values):
    return {'id': fid, 'formula': formula, 'unit': 'tokens', 'numerator': numerator,
            'denominator': denominator, 'share': None if denominator == 0 else numerator / denominator,
            'complete': bool(complete), 'values': values, 'provenance': 'computed'}


def facts(rows, *, start, end, snapshot, timezone='UTC', top_n=50,
          context_threshold=200000, contributor_limit=5, filters=None):
    """Compute four deterministic facts from already-pseudonymized usage rows."""
    start_dt, end_dt, snapshot_dt = (_instant(start, 'start'), _instant(end, 'end'), _instant(snapshot, 'snapshot'))
    if end_dt <= start_dt:
        raise ValueError('end must be after start')
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError('timezone must be a valid IANA timezone') from exc
    top_n = _setting_int(top_n, 'top_n', 1)
    if top_n > 10000:
        raise ValueError('top_n must be <= 10000')
    context_threshold = _setting_int(context_threshold, 'context_threshold', 0)
    if context_threshold < 1:
        raise ValueError('context_threshold must be >= 1')
    contributor_limit = _setting_int(contributor_limit, 'contributor_limit', 1)
    if contributor_limit > 100:
        raise ValueError('contributor_limit must be <= 100')
    safe_filters = _safe_filters(filters)

    cleaned = [_clean_row(row) for row in rows]
    duration = end_dt - start_dt
    prev_start, prev_end = start_dt - duration, start_dt
    current_scope = [r for r in cleaned if start_dt <= r['ts'] < end_dt and r['ts'] <= snapshot_dt]
    previous_scope = [r for r in cleaned if prev_start <= r['ts'] < prev_end and r['ts'] <= snapshot_dt]
    synthetic_excluded = sum(r['id_synthetic'] for r in current_scope)
    previous_synthetic = sum(r['id_synthetic'] for r in previous_scope)
    current = [r for r in current_scope if not r['id_synthetic']]
    previous = [r for r in previous_scope if not r['id_synthetic']]
    # The current bundle uses current-window rows; comparison contributors use each window independently.
    overall = _summary(current, synthetic_excluded)
    unlinked = [r for r in current if r['prompt'] is None]
    rolled = sum(r['rolled'] for r in current)
    derived = sum(r['turn_confidence'] == 'derived' for r in current)
    incomplete_requests = sum(not r['complete'] or any(r['tokens'][k] is None for k in _TOKENS) for r in current)
    coverage = {
        'requests': len(current), 'synthetic_excluded': synthetic_excluded,
        'incomplete_requests': incomplete_requests, 'unlinked_requests': len(unlinked),
        'rolled_up_requests': rolled, 'derived_requests': derived,
    }
    previous_coverage = {
        'requests': len(previous), 'synthetic_excluded': previous_synthetic,
        'incomplete_requests': sum(not r['complete'] or any(r['tokens'][k] is None for k in _TOKENS) for r in previous),
        'unlinked_requests': sum(r['prompt'] is None for r in previous),
        'rolled_up_requests': sum(r['rolled'] for r in previous),
        'derived_requests': sum(r['turn_confidence'] == 'derived' for r in previous),
    }

    auto = [r for r in current if r['auto']]
    work = [r for r in current if not r['auto']]
    work_groups = _turn_rows(work)
    work_known = sum(_known_token(r) for r in work if r['prompt'] is not None)
    ranked_work = sorted(work_groups.items(), key=lambda item: (-sum(_known_token(r) for r in item[1]), item[0]))
    concentration_num = sum(sum(_known_token(r) for r in members) for _, members in ranked_work[:top_n])
    concentration_den = work_known
    concentration_complete = bool(work_groups) and not synthetic_excluded and all(r['complete'] and all(r['tokens'][k] is not None for k in _TOKENS)
                                 for r in current if not r['auto'] and r['prompt'] is not None)
    concentration = _fact('token_concentration', 'top_n_linked_work_tokens / all_linked_work_tokens',
                          concentration_num, concentration_den, concentration_complete,
                          {'top_n': top_n, 'turns': len(work_groups),
                           'top_turns': [_turn_summary(turn, members) for turn, members in ranked_work[:min(top_n, contributor_limit)]], 'unlinked': _summary(unlinked),
                           'auto_review': _summary(auto)})

    eligible, excluded_unknown = [], 0
    for row in current:
        if not _is_complete_input(row):
            excluded_unknown += 1
        elif _known_input(row) >= context_threshold:
            eligible.append(row)
    large_input = sum(_known_input(r) for r in eligible)
    total_input = sum(_known_input(r) for r in current)
    full_input = bool(current) and not synthetic_excluded and all(_is_complete_input(r) and r['complete'] for r in current)
    context_turn_groups = _turn_rows(eligible)
    context_ranked = sorted(context_turn_groups.items(), key=lambda item: (-sum(_known_input(r) for r in item[1]), item[0]))
    complete_sizes = sorted(_known_input(r) for r in current if _is_complete_input(r))
    median = None if not complete_sizes else ((complete_sizes[(len(complete_sizes)-1)//2] + complete_sizes[len(complete_sizes)//2]) / 2)
    p90 = None if not complete_sizes else complete_sizes[math.ceil(.9 * len(complete_sizes)) - 1]
    context_fact = _fact('context_volume', 'eligible_known_input / all_known_input', large_input, total_input,
                         full_input, {'threshold': context_threshold, 'eligible_requests': len(eligible),
                                      'excluded_unknown_input': excluded_unknown, 'median': median, 'p90': p90,
                                      'large': _summary(eligible),
                                      'top_turns': [_turn_summary(t, members) for t, members in context_ranked[:contributor_limit]]})

    subagent = [r for r in work if r['thread_kind'] == 'subagent']
    main = [r for r in work if r['thread_kind'] == 'main']
    other = [r for r in work if r['thread_kind'] not in ('main', 'subagent')]
    sub_groups = _turn_rows(subagent)
    sub_ranked = sorted(sub_groups.items(), key=lambda item: (-sum(_known_token(r) for r in item[1]), item[0]))
    sub_num = sum(_known_token(r) for r in subagent)
    delegation_den = overall['known_tokens']
    delegation_complete = bool(current) and not synthetic_excluded and all(r['complete'] and all(r['tokens'][k] is not None for k in _TOKENS) for r in current)
    delegation = _fact('delegation_volume', 'subagent_work_tokens / all_known_tokens', sub_num, delegation_den,
                       delegation_complete, {'main': _summary(main), 'subagent': _summary(subagent),
                                             'auto_review': _summary(auto), 'other': _summary(other),
                                             'top_turns': [_turn_summary(t, members) for t, members in sub_ranked[:contributor_limit]]})

    prev_by_project, now_by_project, prev_by_turn, now_by_turn = {}, {}, {}, {}
    def accumulate(pool, projects, turns):
        for r in pool:
            value = _known_token(r)
            if r['project_id'] is not None:
                projects[r['project_id']] = projects.get(r['project_id'], 0) + value
            if r['prompt'] is not None:
                turns[r['prompt']] = turns.get(r['prompt'], 0) + value
    accumulate(previous, prev_by_project, prev_by_turn)
    accumulate(current, now_by_project, now_by_turn)
    prev_total = _summary(previous, previous_synthetic)['known_tokens']
    now_total = overall['known_tokens']
    project_deltas = []
    for code in set(prev_by_project) | set(now_by_project):
        before, after = prev_by_project.get(code, 0), now_by_project.get(code, 0)
        delta = after - before
        if delta:
            project_deltas.append({'project': code, 'current': after, 'previous': before, 'delta': delta})
    project_deltas.sort(key=lambda v: (-abs(v['delta']), int(v['project'].split()[-1])))
    turn_deltas = []
    for turn in set(prev_by_turn) | set(now_by_turn):
        before, after = prev_by_turn.get(turn, 0), now_by_turn.get(turn, 0)
        delta = after - before
        if delta:
            turn_deltas.append({'turn': turn, 'current': after, 'previous': before, 'delta': delta})
    turn_deltas.sort(key=lambda v: (-abs(v['delta']), v['turn']))
    delta = now_total - prev_total
    current_complete = bool(current) and not synthetic_excluded and all(r['complete'] and all(r['tokens'][k] is not None for k in _TOKENS) for r in current)
    previous_complete = bool(previous) and not previous_synthetic and all(r['complete'] and all(r['tokens'][k] is not None for k in _TOKENS) for r in previous)
    change = {'id': 'token_change', 'formula': 'current_known_tokens - previous_known_tokens', 'unit': 'tokens',
              'numerator': delta, 'denominator': prev_total, 'share': None if prev_total == 0 else delta / prev_total,
              'complete': current_complete and previous_complete and snapshot_dt >= end_dt,
              'values': {'previous': _summary(previous, previous_synthetic), 'projects': project_deltas[:contributor_limit],
                         'turns': turn_deltas[:contributor_limit], 'comparison': 'equal_elapsed_windows'},
              'provenance': 'computed'}

    return {
        'schema_version': 1,
        'window': {'start': _iso(start_dt), 'end': _iso(end_dt), 'snapshot': _iso(snapshot_dt),
                   'timezone': timezone, 'partial': snapshot_dt < end_dt},
        'previous_window': {'start': _iso(prev_start), 'end': _iso(prev_end), 'partial': snapshot_dt < prev_end},
        'settings': {'top_n': top_n, 'context_threshold': context_threshold, 'contributor_limit': contributor_limit},
        'filters': safe_filters, 'coverage': coverage, 'previous_coverage': previous_coverage, 'totals': overall,
        'facts': [concentration, context_fact, delegation, change],
        'scenarios': {'populations': {'large_context_input': context_fact['values']['large'],
                                      'subagent_input': _summary(subagent)},
                      'overlap': True, 'semantics': 'hypothetical_input_reduction_not_savings'},
    }


def scenario(bundle, population, percent):
    """Estimate an explicit hypothetical input reduction for one named population."""
    if population not in ('large_context_input', 'subagent_input'):
        raise ValueError('population must be large_context_input or subagent_input')
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError('percent must be a finite number from 0 to 100')
    try:
        summary = bundle['scenarios']['populations'][population]
    except (KeyError, TypeError) as exc:
        raise ValueError('invalid efficiency bundle') from exc
    input_tokens = summary['known_input']
    return {'population': population, 'percent': percent, 'input_tokens': input_tokens,
            'hypothetical_reduction': input_tokens * percent / 100,
            'formula': 'known_input * percent / 100', 'complete': summary['complete'],
            'overlap': True, 'semantics': 'hypothetical_input_reduction_not_savings'}


def rows_from_records(records, timezone='UTC', assigned=None):
    """Build only the efficiency allowlist, with global report turn/project codes.

    Call this with the full record universe before report filters are applied;
    `assigned`, when supplied, must be parallel to that same universe.
    """
    zone = ZoneInfo(timezone)
    pairs = list(enumerate(records))
    pairs.sort(key=lambda pair: (pair[1]['ts'], pair[1]['harness'], pair[1]['id']))
    ordered = [r for _, r in pairs]
    if assigned is None:
        assignments = prompts.assign_prompts(ordered)
    else:
        if len(assigned) != len(records):
            raise ValueError('assigned must be parallel to records')
        assignments = [assigned[i] for i, _ in pairs]
    shown = {}
    projects = {}
    out = []
    for record, found in zip(ordered, assignments):
        key = tuple(found[:3]) if found else None
        if key is not None and key not in shown:
            shown[key] = len(shown)
        raw_project = record.get('project_id')
        if raw_project not in (None, '', 'unknown') and raw_project not in projects:
            projects[raw_project] = f'Project {len(projects)+1:03d}'
        ts = datetime.fromisoformat(record['ts'].replace('Z', '+00:00')).astimezone(zone)
        out.append({'ts': ts.isoformat(), 'tokens': {k: (record.get('tokens') or {}).get(k) for k in (*_TOKENS, 'reasoning')},
                    'prompt': shown.get(key), 'project_id': (f'\x01p{int(projects[raw_project].split()[-1]):03d}' if raw_project in projects else None),
                    'complete': bool(record.get('complete')), 'id_synthetic': bool(record.get('id_synthetic')),
                    'thread_kind': record.get('thread_kind'), 'turn_confidence': record.get('turn_confidence'),
                    'efficiency_auto_review': record.get('model') == 'codex-auto-review',
                    'efficiency_rolled_up': bool(found and found[3] == 'rolled_up')})
    return out
