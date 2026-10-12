"""Subscription plan attribution and plan-partitioned cost facts.

Plan names are provider metadata.  A request inherits a Codex plan only from
the closest dated snapshot in the same harness and session; no time window is
assumed because history can be sparse.  The functions here are pure with
respect to their record inputs.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone

from tokenatlas import insights, prompts, plan_metadata


UNKNOWN = "unknown"
PUBLIC_PLANS = frozenset(("free", "plus", "pro", "prolite", "team", "business", "enterprise", "max", "max-5x", "max-20x", UNKNOWN))
_LABELS = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Business",
    "business": "Business",
    "enterprise": "Enterprise",
    "max": "Max",
    "max-5x": "Max 5×",
    "max-20x": "Max 20×",
    UNKNOWN: "Unknown",
}


def _plan(value):
    """Return a canonical plan string, or ``None`` when no plan is known."""
    return plan_metadata.canonical(value)


def _own_plan(record):
    return plan_metadata.own_plan(record)


def _utc(value):
    """Parse an aware timestamp as UTC; malformed and naive values are unknown."""
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if found.tzinfo is None:
        return None
    try:
        return found.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def annotate(records):
    """Return shallow copies of *records* with a canonical ``plan`` field.

    A direct quota ``plan_type`` always wins.  Missing Codex plans inherit the
    nearest dated direct snapshot for the exact ``(harness, session)`` pair;
    equal-distance snapshots prefer the earlier timestamp and then input
    order.  Other harnesses remain unknown unless their own row has a plan.
    """
    rows = list(records)
    direct = [_own_plan(record) for record in rows]
    snapshots = {}
    for index, (record, plan) in enumerate(zip(rows, direct)):
        stamp = _utc(record.get("ts"))
        harness, session = record.get("harness"), record.get("session")
        if plan is None or stamp is None or harness != "codex" or not session:
            continue
        snapshots.setdefault((harness, session), {}).setdefault(stamp, (index, plan))
    # Keep one deterministic snapshot per timestamp, then use the two
    # neighboring timestamps for each lookup instead of scanning a session.
    indexed_snapshots = {
        key: (times := sorted(by_time), [by_time[stamp] for stamp in times])
        for key, by_time in snapshots.items()
    }

    output = []
    for index, (record, own) in enumerate(zip(rows, direct)):
        plan = own
        if plan is None and record.get("harness") == "codex" and record.get("session"):
            stamp = _utc(record.get("ts"))
            indexed = indexed_snapshots.get((record.get("harness"), record.get("session"))) if stamp is not None else None
            if indexed:
                times, found = indexed
                position = bisect_left(times, stamp)
                candidates = []
                if position:
                    snapshot_stamp, (snapshot_index, snapshot_plan) = times[position - 1], found[position - 1]
                    candidates.append((snapshot_stamp, snapshot_index, snapshot_plan))
                if position < len(times):
                    snapshot_stamp, (snapshot_index, snapshot_plan) = times[position], found[position]
                    candidates.append((snapshot_stamp, snapshot_index, snapshot_plan))
                # Earlier wins an equal-distance tie; original position makes
                # duplicate snapshots at one timestamp deterministic.
                stamp, _snapshot_index, plan = min(
                    candidates,
                    key=lambda candidate: (
                        abs((candidate[0] - stamp).total_seconds()),
                        candidate[0] > stamp,
                        candidate[0],
                        candidate[1],
                    ),
                )
        copied = dict(record)
        copied["plan"] = plan or UNKNOWN
        output.append(copied)
    return output


def label(plan):
    """Return a public display label while preserving unknown custom names."""
    if not isinstance(plan, str) or not plan.strip():
        return "Unknown"
    canonical = plan.strip().lower()
    return _LABELS.get(canonical, plan.strip())


def annotate_with_events(records, events=()):
    """Resolve requests and zero-token quota events against one dated universe.

    The same exact Codex harness/session inference applies to both. Events
    remain events: quota readings, tokens and account identity are untouched.
    Preserve a known additive plan supplied by a larger, previously annotated
    universe when this caller receives only a filtered subset.
    """
    requests, observations = list(records), list(events)
    source = requests + observations
    if all(row.get('plan') == (plan_metadata.plan_of(row) or UNKNOWN) for row in source):
        return requests, observations
    annotated = annotate(source)
    for original, copied in zip(source, annotated):
        if _own_plan(original) is None and (supplied := plan_metadata.plan_of(original)):
            copied['plan'] = supplied
    return annotated[:len(requests)], annotated[len(requests):]


def plan_facts(records, table, assigned=None):
    """Partition observations by plan and sum complete USD list-price costs.

    ``priced`` and ``unpriced`` count requests.  ``turns`` counts distinct
    prompt assignments, so a turn containing requests from multiple plans is
    counted in each relevant plan bucket.
    """
    source = list(records)
    direct = [_own_plan(record) for record in source]
    supplied = [_plan(record.get("plan")) for record in source]
    if any(own is None and given is None for own, given in zip(direct, supplied)):
        # A filtered report may receive rows annotated against a larger universe.
        # Keep that additive result when no direct quota plan is present; direct
        # quota metadata remains authoritative if a caller supplied a stale plan.
        rows = annotate(source)
        plans = [own or given or annotated["plan"]
                 for own, given, annotated in zip(direct, supplied, rows)]
    else:
        rows = source
        plans = [own or given for own, given in zip(direct, supplied)]
    assignments = prompts.assign_prompts(source) if assigned is None else assigned
    memo = {}
    groups = {}
    for record, found, plan in zip(rows, assignments, plans):
        if record.get('id_synthetic'):continue  # the report and insights exclude ambiguous identities
        group = groups.setdefault(plan, {"observations": 0, "turns": set(), "cost": 0.0, "priced": 0, "unpriced": 0, "lower_bound": False})
        group["observations"] += 1
        group['lower_bound'] = group['lower_bound'] or not record.get('complete', True)
        if found:
            group["turns"].add(tuple(found[:3]))
        _record, result, cost, _tier = insights.memo_row(record, table, memo)
        if cost is None:
            group["unpriced"] += 1
            group['lower_bound'] = True
        else:
            group["priced"] += 1
            group["cost"] += cost

    plans = []
    for plan in sorted(groups, key=lambda value: (value == UNKNOWN, value)):
        group = groups[plan]
        plans.append({"plan": plan, "label": label(plan), "observations": group["observations"],
                      "turns": len(group["turns"]), "cost": group["cost"] if group["priced"] else None,
                      "priced": group["priced"], "unpriced": group["unpriced"], "lower_bound": group['lower_bound']})
    return {
        "plans": plans,
        "note": "A turn that contains requests under multiple plans is counted once in each plan bucket; plan turn counts may overlap.",
    }
