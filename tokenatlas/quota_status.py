"""Raw, account-wide quota readings for report status cards.

This module deliberately does not use :mod:`tokenatlas.quota_share`: quota
shares need segmented windows and calibration, while the status card needs the
last reading the providers actually reported.  The inputs are already local
history records and the private Claude statusline sidecar. A named rejected
Claude limit is already normalized by the collector to an exhausted (100%)
window; it is factual exhaustion evidence, not a cost-based estimate.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path


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


def _scope(harness, quota):
    """Return the stable internal scope key and payload fields."""
    limit_id = quota.get("limit_id") if isinstance(quota, dict) else None
    if not isinstance(limit_id, str) or not limit_id.strip() or limit_id == harness:
        account = None
    else:
        account = limit_id
    plan = quota.get("plan_type") if isinstance(quota, dict) else None
    if not isinstance(plan, str) or not plan.strip():
        plan = None
    return account, plan


def _consider(found, *, harness, account, plan, minutes, used, resets, observed):
    if harness not in ("claude", "codex") or minutes not in WINDOWS or observed is None:
        return
    key = (harness, account, plan, minutes)
    candidate = dict(harness=harness, account=account, plan_type=plan, minutes=minutes,
                     used_percent=_percent(used), resets_at=_utc_iso(resets),
                     observed_at=observed.isoformat())
    previous = found.get(key)
    if previous is None or observed >= previous[0]:
        found[key] = (observed, candidate)


def _record_readings(found, items, now):
    for record in items or ():
        if not isinstance(record, dict):
            continue
        observed = _time(record.get("ts"))
        if observed is None or observed > now:
            continue
        harness = record.get("harness")
        quota = record.get("quota")
        if harness not in ("claude", "codex") or not isinstance(quota, dict):
            continue
        account, plan = _scope(harness, quota)
        windows = quota.get("windows")
        if not isinstance(windows, (list, tuple)):
            continue
        for window in windows:
            if not isinstance(window, dict):
                continue
            minutes = window.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in WINDOWS:
                continue
            _consider(found, harness=harness, account=account, plan=plan, minutes=minutes,
                      used=window.get("used_percent"), resets=window.get("resets_at"),
                      observed=observed)


def _claude_readings(found, path, now):
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
                _consider(found, harness="claude", account=None, plan=None, minutes=minutes,
                          used=window.get("used_percent"), resets=window.get("resets_at"),
                          observed=observed)


def collect(records=(), events=(), claude=None, now=None):
    """Return latest raw quota rows keyed by provider scope and duration.

    ``records`` and ``events`` must be the account-wide universe selected by
    the caller.  No percentage is derived, clamped, peaked, or calibrated;
    future observations are ignored.  The returned rows are deterministic.
    """
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    found = {}
    _record_readings(found, records, now)
    _record_readings(found, events, now)
    _claude_readings(found, claude, now)
    return [row for _, row in sorted(found.values(), key=lambda item: (
        item[1]["harness"], item[1]["account"] or "", item[1]["plan_type"] or "", item[1]["minutes"]))]


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
