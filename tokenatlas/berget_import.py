"""Import explicitly matched Berget response emissions without retaining response content.

Each JSONL row names the harness request already present in local history.  Only
the allowlisted ``usage.emissions`` object is saved; prompts and response bodies
are neither copied into history nor returned in diagnostics.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


MAX_LINE_BYTES = 65536
ALLOWED_HARNESSES = frozenset(('pi', 'opencode'))


def _parse_line(raw: bytes, number: int):
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError(f'line {number}: exceeds {MAX_LINE_BYTES} bytes')
    try:
        row = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f'line {number}: invalid JSON') from exc
    if not isinstance(row, dict):
        raise ValueError(f'line {number}: expected a JSON object')
    harness, request_id, model = (row.get(key) for key in ('harness', 'request_id', 'model'))
    if harness not in ALLOWED_HARNESSES or not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise ValueError(f'line {number}: expected pi/opencode harness and bounded request_id')
    if not isinstance(model, str) or not model or len(model) > 256:
        raise ValueError(f'line {number}: model is required')
    try:
        timestamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError
        timestamp = timestamp.astimezone(timezone.utc).isoformat()
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f'line {number}: timestamp must include a UTC offset') from exc
    usage = row.get('usage')
    if not isinstance(usage, dict) or 'emissions' not in usage:
        raise ValueError(f'line {number}: usage.emissions is required')
    from tokenatlas.history import clean_environment
    environment = clean_environment(usage['emissions'])
    if environment is None:
        raise ValueError(f'line {number}: invalid usage.emissions')
    return (harness, request_id), (model, timestamp, environment)


def import_emissions(history, path: Path):
    """Atomically attach final provider estimates to matching Berget observations.

    Repeated identical rows are harmless. Conflicting estimates or ambiguous IDs
    abort the entire file rather than silently selecting a value.
    """
    from tokenatlas.history import _encode, _key
    path = Path(path).expanduser()
    parsed = {}
    try:
        with path.open('rb') as stream:
            for number, raw in enumerate(stream, 1):
                key, value = _parse_line(raw, number)
                if key in parsed and parsed[key] != value:
                    raise ValueError(f'line {number}: conflicting duplicate request')
                parsed[key] = value
    except OSError as exc:
        raise ValueError(f'cannot read emissions file: {type(exc).__name__}') from exc
    if not parsed:
        raise ValueError('emissions file is empty')
    connection = history.connection
    connection.execute('BEGIN IMMEDIATE')
    history._strings, history._values = {}, {}
    try:
        records = history.records(include_limit_events=True)
        matches = {}
        for record in records:
            key = (record['harness'], record['id'])
            if key in parsed and record['provider'] == 'berget' and not record['id_synthetic']:
                matches.setdefault(key, []).append(record)
        changed = []
        for key, (model, timestamp, environment) in parsed.items():
            candidates = matches.get(key, [])
            if len(candidates) != 1:
                raise ValueError(f'{key[0]} request has {len(candidates)} matching Berget observations')
            item = candidates[0]
            if item['model'] != model or item['ts'] != timestamp:
                raise ValueError(f'{key[0]} request model or timestamp does not match history')
            old = item.get('environment')
            if old is not None and old != environment:
                raise ValueError(f'{key[0]} request already has a different emissions estimate')
            if old is None:
                item = dict(item)
                item.pop('sources', None)
                item['environment'] = environment
                changed.append(item)
        for item in changed:
            history._insert(connection, _encode(item, _key(item)))
        if changed:
            history._bump(connection)
        connection.commit()
        return {'requests': len(parsed), 'updated': len(changed), 'unchanged': len(parsed) - len(changed)}
    except BaseException:
        connection.rollback()
        history._strings, history._values = {}, {}
        raise
