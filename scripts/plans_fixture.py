#!/usr/bin/env python3
"""Write a deterministic offline report fixture for subscription-plan UI checks."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_report import observation
from tokenatlas.report import build_report, render_report, write_report


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def quota(plan: str | None, used: int = 20) -> dict:
    body = {"plan_type": plan, "windows": [{"minutes": 300, "used_percent": used,
                                             "resets_at": "2026-10-12T14:00:00+00:00"}]}
    return body if plan is not None else {"windows": body["windows"]}


def records() -> list[dict]:
    rows = []
    rows.append(observation("business-1", ts="2026-10-10T10:00:00+00:00", harness="codex", provider="openai",
                            session="business", model="gpt-5.5", quota=quota("business", 12),
                            tokens={"fresh_input": 1000, "cache_read": 0, "cache_write": 0, "output": 100, "reasoning": 0}))
    rows.append(observation("business-2", ts="2026-10-11T10:00:00+00:00", harness="codex", provider="openai",
                            session="business", model="gpt-5.5", quota=quota(None, 18),
                            tokens={"fresh_input": 2000, "cache_read": 0, "cache_write": 0, "output": 200, "reasoning": 0}))
    rows.append(observation("pro-1", ts="2026-10-10T11:00:00+00:00", harness="codex", provider="openai",
                            session="pro", model="gpt-5.5", quota=quota("pro", 22),
                            tokens={"fresh_input": 3000, "cache_read": 0, "cache_write": 0, "output": 300, "reasoning": 0}))
    rows.append(observation("pro-2", ts="2026-10-11T11:00:00+00:00", harness="codex", provider="openai",
                            session="pro", model="gpt-5.5", quota=quota(None, 27),
                            tokens={"fresh_input": 4000, "cache_read": 0, "cache_write": 0, "output": 400, "reasoning": 0}))
    rows.append(observation("unknown-1", ts="2026-10-11T12:00:00+00:00", harness="codex", provider="openai",
                            session="unknown", model="gpt-5.5", quota=quota(None, 31),
                            tokens={"fresh_input": 5000, "cache_read": 0, "cache_write": 0, "output": 500, "reasoning": 0}))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="plans-report.html")
    parser.add_argument("--shared", action="store_true")
    args = parser.parse_args()
    report = build_report(records(), {}, redact=args.shared, now=NOW)
    write_report(Path(args.output), render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
