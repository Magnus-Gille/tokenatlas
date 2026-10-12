#!/usr/bin/env python3
"""Create a deterministic quota_status fixture or inject it into a report HTML file.

Usage:
  python3 scripts/subscriptions_fixture.py > quota_status.json
  python3 scripts/subscriptions_fixture.py REPORT.html OUTPUT.html

The second form only replaces the compressed report payload; it does not make any
network requests or read a user's logs.
"""
from __future__ import annotations

import base64
import gzip
import json
import re
import sys
from pathlib import Path


ROWS = [
    # An older reading for this scope/window proves that the page keeps the latest one.
    {"harness": "codex", "account": None, "plan_type": "pro", "minutes": 300,
     "used_percent": 31, "resets_at": "2026-10-12T13:00:00Z", "observed_at": "2026-10-12T10:00:00Z"},
    {"harness": "codex", "account": None, "plan_type": "pro", "minutes": 300,
     "used_percent": 42, "resets_at": "2026-10-12T14:00:00Z", "observed_at": "2026-10-12T11:45:00Z"},
    {"harness": "codex", "account": None, "plan_type": "pro", "minutes": 10080,
     "used_percent": 78, "resets_at": "2026-10-13T12:00:00Z", "observed_at": "2026-10-12T10:00:00Z"},
    {"harness": "claude", "account": "work", "plan_type": "max", "minutes": 300,
     "used_percent": None, "resets_at": None, "observed_at": "2026-10-12T11:50:00Z"},
    {"harness": "claude", "account": "work", "plan_type": "max", "minutes": 10080,
     "used_percent": 100, "resets_at": "2026-10-12T11:00:00Z", "observed_at": "2026-10-12T11:50:00Z"},
    {"harness": "claude", "account": "work", "plan_type": "legacy", "minutes": 300,
     "used_percent": 12, "resets_at": "2026-10-12T15:00:00Z", "observed_at": "2026-10-12T10:00:00Z"},
]


def inject(html: str, rows: list[dict]) -> str:
    pattern = re.compile(
        r"(<script id=\"report-data\" type=\"application/octet-stream\+base64\">)"
        r"([A-Za-z0-9+/=]+)(</script>)"
    )

    def replace(match: re.Match[str]) -> str:
        payload = json.loads(gzip.decompress(base64.b64decode(match.group(2))))
        payload["quota_status"] = rows
        encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(",", ":")).encode())).decode()
        return match.group(1) + encoded + match.group(3)

    out, count = pattern.subn(replace, html, count=1)
    if count != 1:
        raise ValueError("report HTML has no compressed report-data payload")
    return out


def main() -> int:
    if len(sys.argv) == 1:
        json.dump(ROWS, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, destination = map(Path, sys.argv[1:])
    destination.write_text(inject(source.read_text(), ROWS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
