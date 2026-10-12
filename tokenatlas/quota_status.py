"""Raw, account-wide quota readings for report status cards.

The status card needs the last reading the providers actually reported. Optional
quota-share snapshots provide counter identity only, never percentages or reset
values. The inputs are already local
history records and the private Claude statusline sidecar. A named rejected
Claude limit is already normalized by the collector to an exhausted (100%)
window; it is factual exhaustion evidence, not a cost-based estimate.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import plan_metadata


WINDOWS = frozenset((300, 10080))
_WINDOW_NAMES = (("five_hour", 300), ("seven_day", 10080))


def _time(value):
    """Return an aware UTC datetime, or ``None`` for an unusable timestamp."""
    if isinstance(value, datetime):
        found = value
    elif isinstance(value, str):
        try:
            found = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if found.tzinfo is None:
        return None
    try:
        return found.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _utc_iso(value):
    """Canonical UTC ISO text for a reset value, preserving missing resets."""
    found = _time(value)
    if found is not None:
        return found.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return None


def _percent(value):
    """A finite nonnegative provider percentage, including overshoot, or ``None``.

    Invalid values still produce a row when the provider named a valid window;
    that preserves the distinction between an unknown reading and a real zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if value < 0 or not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def _scope(harness, quota, record=None):
    """Return the stable internal scope key and payload fields."""
    limit_id = quota.get("limit_id") if isinstance(quota, dict) else None
    if not isinstance(limit_id, str) or not limit_id.strip() or limit_id == harness:
        account = None
    else:
        account = limit_id
    plan = plan_metadata.plan_of(record if record is not None else {'quota': quota})
    return account, plan


def _consider(found, *, harness, account, plan, minutes, used, resets, observed, inferred=None):
    if harness not in ("claude", "codex") or minutes not in WINDOWS or observed is None:
        return
    key = (harness, account, plan, minutes, inferred)
    candidate = dict(harness=harness, account=account, plan_type=plan, minutes=minutes,
                     used_percent=_percent(used), resets_at=_utc_iso(resets),
                     observed_at=observed.isoformat())
    if inferred is not None:candidate['inferred_account'] = inferred
    previous = found.get(key)
    if previous is None or observed >= previous[0]:
        found[key] = (observed, candidate)


def _record_readings(found, items, now, counters=None, excluded=frozenset()):
    for index, record in enumerate(items or ()):
        if not isinstance(record, dict):
            continue
        observed = _time(record.get("ts"))
        if observed is None or observed > now:
            continue
        harness = record.get("harness")
        quota = record.get("quota")
        if harness not in ("claude", "codex") or not isinstance(quota, dict):
            continue
        account, plan = _scope(harness, quota, record)
        windows = quota.get("windows")
        if not isinstance(windows, (list, tuple)):
            continue
        for window in windows:
            if not isinstance(window, dict):
                continue
            minutes = window.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in WINDOWS:
                continue
            if (index, minutes) in excluded:continue
            _consider(found, harness=harness, account=account, plan=plan, minutes=minutes,
                      used=window.get("used_percent"), resets=window.get("resets_at"),
                      observed=observed, inferred=(counters or {}).get((index, minutes)))


def _sidecar_key(observed, session, minutes, used, resets):
    return (observed.isoformat(), session if isinstance(session, str) else None,
            minutes, _percent(used), _utc_iso(resets))


def _snapshot_source(snapshot):
    """Identify the original reading, not the request a sidecar was attached to."""
    minutes = snapshot['key'][2]
    if snapshot.get('source') == 'claude_sidecar':
        return 'claude', _sidecar_key(_time(snapshot['t']), snapshot.get('source_session'),
                                     minutes, snapshot['used_percent'], snapshot['resets'])
    if snapshot.get('event_i') is not None:return 'event', (snapshot['event_i'], minutes)
    if snapshot['i'] is not None:return 'record', (snapshot['i'], minutes)
    return None, None


def _claude_readings(found, path, now, counters=None, excluded=frozenset()):
    """Read explicit Claude five-hour and seven-day statusline windows.

    ``quota_share.read_claude`` requires a reset to attach a reading to a
    window instance.  The status payload does not: a missing reset is retained
    as unknown context while its percentage remains useful.
    """
    if not path:
        return
    try:
        stream = Path(path).open(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return
    with stream:
        for text in stream:
            try:
                row = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            observed = _time(row.get("ts"))
            if observed is None or observed > now:
                continue
            for name, minutes in _WINDOW_NAMES:
                window = row.get(name)
                if not isinstance(window, dict):
                    continue
                key = _sidecar_key(observed, row.get('session'), minutes, window.get('used_percent'), window.get('resets_at'))
                if key in excluded:continue
                _consider(found, harness="claude", account=None, plan=None, minutes=minutes,
                          used=window.get("used_percent"), resets=window.get("resets_at"),
                          observed=observed, inferred=(counters or {}).get(key))


def collect(records=(), events=(), claude=None, now=None, snapshots=None):
    """Return latest raw quota rows keyed by provider scope and duration.

    ``records`` and ``events`` must be the account-wide universe selected by
    the caller.  No percentage is derived, clamped, peaked, or calibrated;
    future observations are ignored.  The returned rows are deterministic.
    """
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    found = {}
    counters = {kind: {} for kind in ('record', 'event', 'claude')}
    excluded = {kind: set() for kind in counters}
    latest_instances = {}
    instances = {}
    for snapshot in [*(snapshots or ()), *getattr(snapshots, 'events', ())]:
        if snapshot['t'] > now:continue
        key = snapshot['key']
        scope = (*key[:3], key[4])
        instance = (*scope, key[6])
        instances.setdefault(instance, []).append(snapshot)
        old = latest_instances.get(scope)
        # A delayed reading may belong to an older instance after a reset.
        # Segment sequence, not observation timestamp, identifies the current one.
        if old is None or key[6] > old[0]:latest_instances[scope] = (key[6], instance)
    for instance, group in instances.items():
        if instance != latest_instances[instance[:-1]][1]:
            for s in group:
                kind, identity = _snapshot_source(s)
                if kind is not None:excluded[kind].add(identity)
    for _, instance in latest_instances.values():
        group = instances[instance]
        if len({s['key'][5] for s in group}) > 1:
            for s in group:
                kind, identity = _snapshot_source(s)
                if kind is not None:counters[kind][identity] = s['key'][5] + 1
    _record_readings(found, records, now, counters['record'], excluded['record'])
    _record_readings(found, events, now, counters['event'], excluded['event'])
    _claude_readings(found, claude, now, counters['claude'], excluded['claude'])
    # Earlier unsplit readings are superseded by the proven current counters.
    # A newer unreadable/unassigned reading stays visible as unknown scope.
    for key, value in list(found.items()):
        if key[4] is None:
            split = [v[0] for k, v in found.items() if k[:4] == key[:4] and k[4] is not None]
            if split and max(split) >= value[0]:del found[key]
    return [row for _, row in sorted(found.values(), key=lambda item: (
        item[1]["harness"], item[1]["account"] or "", item[1]["plan_type"] or "", item[1].get('inferred_account', 0), item[1]["minutes"]))]


def redact(rows, shared=False, account_alias=None, plan_alias=None):
    """Apply the report's existing alias boundary to non-default labels."""
    if not shared:
        return [dict(row) for row in rows]
    if account_alias is None or plan_alias is None:
        raise ValueError("shared quota status requires account and plan alias callbacks")
    out = []
    for row in rows:
        value = dict(row)
        if value["account"] is not None:
            value["account"] = account_alias(value["account"])
        if value["plan_type"] is not None:
            value["plan_type"] = plan_alias(value["plan_type"])
        out.append(value)
    return out
