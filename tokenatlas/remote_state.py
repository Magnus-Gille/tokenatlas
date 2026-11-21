"""Per-host state written by remote_sync.sh (TOKEN-198): consecutive failures, last success and the back-off deadline.

The script keeps one tiny ``<tag>.sync-state`` file per host in ``~/.local/state/tokenatlas/remote``. This module only reads
them, for ``doctor`` and the report; a missing or unreadable file is simply no information."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

_TAG = re.compile(r'[A-Za-z0-9_-]+')


def remote_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / '.local' / 'state' / 'tokenatlas' / 'remote'


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if epoch else None


def read(home: Path | None = None, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    try:
        files = sorted(remote_dir(home).glob('*.sync-state'))
    except OSError:
        return out
    for path in files:
        tag = path.name[:-len('.sync-state')]
        if not _TAG.fullmatch(tag):
            continue
        try:
            fields = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
            fails, last_ok, last_try, next_try = (int(fields.get(k, 0)) for k in ('fails', 'last_ok', 'last_try', 'next_try'))
        except (OSError, ValueError):
            continue
        out.append(dict(host=tag, consecutive_failures=fails, last_success=_iso(last_ok), last_attempt=_iso(last_try),
                        next_attempt=_iso(next_try) if next_try > now else None, backing_off=next_try > now))
    return out


def failing(entries) -> list[dict]:
    """The hosts to flag in a report: stable fields only, so an unchanged situation does not rebuild the report."""
    return [dict(host=e['host'], consecutive_failures=e['consecutive_failures'], last_success=e['last_success'])
            for e in entries or [] if e.get('consecutive_failures', 0) > 0]
