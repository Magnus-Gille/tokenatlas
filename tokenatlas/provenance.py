"""Source provenance: imported source references are '<machine>:<path>', locally collected ones are absolute paths."""
from __future__ import annotations

import os
import re
from pathlib import Path

MACHINE_ID = re.compile(r'm-[0-9a-f]{32}')  # exactly what History generates
REMOTE_PATH = re.compile(r'm-[0-9a-f]{32}:')  # imported source paths are '<machine>:<path>'; local ones are absolute
_DRIVE = re.compile(r'[A-Za-z]:[\\/]')


def is_machine_id(value: object) -> bool:
    return isinstance(value, str) and MACHINE_ID.fullmatch(value) is not None


def _plain(text: str) -> bool:
    """Not an imported-looking reference: no ':' before the first separator, except a Windows drive on Windows."""
    head = re.split(r'[\\/]', text, maxsplit=1)[0]
    return ':' not in head or (os.name == 'nt' and _DRIVE.match(text) is not None and head.count(':') == 1)


def local_file(source: object) -> Path | None:
    """The existing regular file of a locally collected source, else None.

    Rejects imported references (machine prefix, whatever the observation's machine field says), relative paths
    and anything that is not an existing regular file, so such references never reach a local reader."""
    if not isinstance(source, (str, Path)):
        return None
    text = str(source)
    if not text or '\0' in text or REMOTE_PATH.match(text) or not _plain(text):
        return None
    path = Path(text)
    try:
        return path if path.is_absolute() and path.is_file() else None
    except (OSError, ValueError):
        return None


def local_sources(sources: object) -> list[str]:
    """Only the local, absolute source paths from an observation's source list."""
    items = sources if isinstance(sources, (list, tuple, set)) else [sources]
    return [s for s in items if isinstance(s, str) and s and not REMOTE_PATH.match(s) and _plain(s)
            and Path(s).is_absolute()]
