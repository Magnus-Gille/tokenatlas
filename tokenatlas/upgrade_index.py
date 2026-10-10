"""Small, opt-in PyPI lookup used by the upgrade command."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable


PYPI_JSON_URL = "https://pypi.org/pypi/tokenatlas/json"
_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 1024 * 1024
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\Z")
_PYTHON_COMPONENT = r"(?:0|[1-9][0-9]{0,8})"
_PYTHON_CLAUSE_RE = re.compile(rf"(==|!=|<=|>=|<|>)\s*({_PYTHON_COMPONENT}(?:\.{_PYTHON_COMPONENT}){{0,2}}(?:\.\*)?)\Z")


def validate_version(value: str) -> str:
    """Return a strict stable ``N.N.N`` version or raise ``ValueError``."""
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise ValueError("Version must be a stable numeric N.N.N release (for example 1.2.3).")
    return value


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _python_compatible(specifier: str | None, current: tuple[int, int, int]) -> bool:
    """Evaluate a conservative subset of Requires-Python; unknown syntax fails closed."""
    if specifier is None or specifier == "":
        return True
    if not isinstance(specifier, str):
        return False

    for raw_clause in specifier.split(","):
        clause = raw_clause.strip()
        if clause.startswith("==="):
            if clause[3:] != ".".join(str(part) for part in current):
                return False
            continue
        match = _PYTHON_CLAUSE_RE.fullmatch(clause)
        if not match:
            return False
        operator, required_text = match.groups()
        wildcard = required_text.endswith(".*")
        if wildcard:
            if operator not in ("==", "!="):
                return False
            prefix_text = required_text[:-2]
            prefix = tuple(int(part) for part in prefix_text.split("."))
            matches = current[:len(prefix)] == prefix
            if (operator == "==" and not matches) or (operator == "!=" and matches):
                return False
            continue

        required_parts = tuple(int(part) for part in required_text.split("."))
        required = required_parts + (0,) * (3 - len(required_parts))
        if operator == "==":
            if current != required:
                return False
        elif operator == "!=":
            if current == required:
                return False
        elif operator == ">=" and current < required:
            return False
        elif operator == ">" and current <= required:
            return False
        elif operator == "<=" and current > required:
            return False
        elif operator == "<" and current >= required:
            return False
    return True


def _fetch_json() -> Any:
    request = urllib.request.Request(
        PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": "tokenatlas-upgrade-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("PyPI version response exceeded the 1 MiB size limit.")
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("PyPI returned an invalid version index.") from exc
    except ValueError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError("Could not retrieve a valid TokenAtlas version from PyPI; check the connection and retry.") from exc


def latest_version(fetch_json: Callable[[str], Any] | None = None) -> str:
    """Return the highest compatible stable version with a usable PyPI file.

    ``fetch_json`` is an optional offline-test seam. It receives the fixed PyPI
    URL and returns its already-decoded JSON value.
    """
    payload = fetch_json(PYPI_JSON_URL) if fetch_json is not None else _fetch_json()
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), dict):
        raise ValueError("PyPI returned an invalid version index.")

    current = tuple(sys.version_info[:3])
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for version, files in payload["releases"].items():
        if not isinstance(version, str) or not isinstance(files, list):
            raise ValueError("PyPI returned an invalid release entry.")
        try:
            stable_version = validate_version(version)
        except ValueError:
            continue

        usable_file = False
        for file_info in files:
            if not isinstance(file_info, dict):
                raise ValueError("PyPI returned invalid release file metadata.")
            filename = file_info.get("filename")
            package_type = file_info.get("packagetype")
            yanked = file_info.get("yanked")
            requires_python = file_info.get("requires_python")
            if (not isinstance(filename, str) or not filename
                    or not isinstance(package_type, str) or not isinstance(yanked, bool)
                    or (requires_python is not None and not isinstance(requires_python, str))):
                raise ValueError("PyPI returned invalid release file metadata.")
            expected_wheel = f"tokenatlas-{stable_version}-py3-none-any.whl"
            if package_type != "bdist_wheel" or filename != expected_wheel or yanked:
                continue
            if _python_compatible(requires_python, current):
                usable_file = True
                break
        if usable_file:
            candidates.append((_version_tuple(stable_version), stable_version))

    if not candidates:
        raise ValueError("PyPI has no stable TokenAtlas release with a universal wheel compatible with this Python version.")
    return max(candidates)[1]
