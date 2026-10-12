"""Canonical provider plan metadata, without report or pricing dependencies."""


def canonical(value):
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def own_plan(record):
    quota = record.get('quota')
    return canonical(quota.get('plan_type')) if isinstance(quota, dict) else None


def plan_of(record):
    """Direct provider metadata wins over an additive inherited plan."""
    plan = own_plan(record) or canonical(record.get('plan'))
    return None if plan == 'unknown' else plan
