"""Collectors for the local coding harnesses (Claude Code, Codex, Pi, OpenCode).

Stateless readers over the harnesses' retained files: one normalised
AttributionRecord per model call. Reasoning tokens are a subset of output and
are never added twice. The module name is historical; the former `why` command
line is retired and only the collectors live here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
# Claude desktop Cowork (macOS only): local_*/.claude/projects hold ordinary Claude Code transcripts.
# Never read the sibling audit.jsonl: it is an SDK stream copy of the same calls under other id keys.
COWORK_SESSIONS = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"


def _env_dir(var, tilde=False):
    """Absolute directory named by `var`, else None; unset, empty and (per the XDG spec) relative values mean the default."""
    value = os.environ.get(var, '')
    # Only "~" and "~/..." mean the home directory (as Pi expands them); "~user/..." stays relative, so the default,
    # on every platform (Windows' expanduser would guess a sibling of the current home).
    if tilde and (value == '~' or value.startswith(('~/', '~\\'))):
        try:
            value = str(Path.home() / value[2:])
        except (RuntimeError, KeyError):  # no home directory to expand: default
            return None
    return Path(value) if value and Path(value).is_absolute() else None


def harness_root(name):
    """(path, source) of a harness's log root, read from the environment at call time; source is 'default' or the variable."""
    for var, harness, tail, default, tilde in (
            ('CLAUDE_CONFIG_DIR', 'claude', ('projects',), CLAUDE_PROJECTS, False),
            ('CODEX_HOME', 'codex', ('sessions',), CODEX_SESSIONS, False),
            ('PI_CODING_AGENT_DIR', 'pi', ('sessions',), PI_SESSIONS, True),
            ('XDG_DATA_HOME', 'opencode', ('opencode', 'opencode.db'), OPENCODE_DB, False)):
        if harness == name:
            base = _env_dir(var, tilde)
            return (base.joinpath(*tail), var) if base else (default, 'default')
    raise ValueError(f'unknown harness: {name}')


def codex_session_index():
    """Codex's session index next to its sessions: CODEX_HOME/session_index.jsonl, else the default."""
    base = _env_dir('CODEX_HOME')
    return (base if base else CODEX_SESSIONS.parent) / 'session_index.jsonl'


def cowork_scan() -> tuple[list[Path], list[str]]:
    """Every <org>/<acct>/local_*/.claude/projects directory plus the errors met while walking; a missing base is empty."""
    roots, errors = [], []
    def entries(path, keep):
        try:
            with os.scandir(path) as it:
                return sorted((e.path for e in it if keep(e)), key=str)
        except FileNotFoundError:
            return []
        except OSError as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return []
    isdir = lambda e: e.is_dir()
    level = [COWORK_SESSIONS]
    for keep in (isdir, isdir, lambda e: e.name.startswith("local_") and e.is_dir()):
        level = [child for parent in level for child in entries(parent, keep)]
    for session in level:
        projects = Path(session) / ".claude" / "projects"
        try:
            mode = os.stat(projects).st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue  # a session without transcripts
        except OSError as exc:
            errors.append(f"{projects}: {type(exc).__name__}: {exc}")
            continue
        if stat.S_ISDIR(mode):
            roots.append(projects)
        else:
            errors.append(f"{projects}: not a directory")
    return sorted(roots), errors


def cowork_roots() -> list[Path]:
    """Every <org>/<acct>/local_*/.claude/projects directory; a missing base is simply empty."""
    return cowork_scan()[0]


@dataclass(frozen=True)
class AttributionRecord:
    harness: str
    provider: str
    timestamp: datetime
    session_id: str
    call_id: str
    model: str
    effort: str
    project: str
    entrypoint: str
    thread_kind: str
    agent: str
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    reasoning: int
    project_id: str = ""
    cwd: str | None = None
    turn_id: str | None = None
    turn_confidence: str = "absent"
    parent_session_id: str | None = None
    harness_version: str | None = None
    session_started_at: datetime | None = None
    raw_usage: dict = field(default_factory=dict)
    id_synthetic: bool = False
    output_final: bool | None = None
    tariff: dict | None = None
    quota: dict | None = None
    flags: list | None = None  # sorted short labels observed on the request, e.g. ['interrupted']; None when none
    # Provider-reported environmental accounting.  This is deliberately kept
    # separate from token usage: an estimate must never be inferred from the
    # TokenAtlas proxy counters.
    environment: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cache_read + self.cache_write + self.output


def parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (OverflowError, TypeError, ValueError):
        return 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


_ENVIRONMENT_TOP_FIELDS = frozenset({
    "co2e_grams", "energy_kwh", "operational", "embodied", "grid",
    "methodology", "methodology_version",
})
_ENVIRONMENT_OUTPUT_FIELDS = _ENVIRONMENT_TOP_FIELDS | {"source"}


def _environment_number(value: object) -> int | float | None:
    """Return a finite, non-negative JSON number, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return value if math.isfinite(value) and value >= 0 else None


def _environment_text(value: object, limit: int = 256) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
        return None
    return value


def clean_environment(value: object, provider: object = "berget") -> dict | None:
    """Sanitize Berget's published ``usage.emissions`` response shape.

    The returned object is intentionally exact and complete.  Unknown keys,
    non-finite/negative numbers, missing nested fields, and providers other
    than Berget are rejected so partial or guessed accounting cannot enter the
    durable history.
    """
    if not isinstance(provider, str) or provider.casefold() != "berget" or not isinstance(value, dict):
        return None
    if not set(value) <= _ENVIRONMENT_OUTPUT_FIELDS or set(value) < _ENVIRONMENT_TOP_FIELDS:
        return None
    if "source" in value and value.get("source") != "provider_reported_estimate":
        return None
    result: dict = {}
    for key in ("co2e_grams", "energy_kwh"):
        number = _environment_number(value.get(key))
        if number is None:
            return None
        result[key] = number
    operational = value.get("operational")
    if not isinstance(operational, dict) or set(operational) != {"co2e_grams", "energy_kwh"}:
        return None
    result["operational"] = {}
    for key in ("co2e_grams", "energy_kwh"):
        number = _environment_number(operational.get(key))
        if number is None:
            return None
        result["operational"][key] = number
    embodied = value.get("embodied")
    if not isinstance(embodied, dict) or set(embodied) != {"co2e_grams"}:
        return None
    embodied_co2e = _environment_number(embodied.get("co2e_grams"))
    if embodied_co2e is None:
        return None
    result["embodied"] = {"co2e_grams": embodied_co2e}
    grid = value.get("grid")
    if not isinstance(grid, dict) or set(grid) != {"region", "carbon_intensity_gco2e_per_kwh"}:
        return None
    region = _environment_text(grid.get("region"))
    intensity = _environment_number(grid.get("carbon_intensity_gco2e_per_kwh"))
    if region is None or intensity is None:
        return None
    result["grid"] = {"region": region, "carbon_intensity_gco2e_per_kwh": intensity}
    methodology = _environment_text(value.get("methodology"))
    methodology_version = _environment_text(value.get("methodology_version"))
    if methodology is None or methodology_version is None:
        return None
    result["methodology"] = methodology
    result["methodology_version"] = methodology_version
    # Upstream rounds CO₂e components to six decimals before adding them.
    # Permit that rounding while rejecting internally contradictory totals.
    co2_parts = result["operational"]["co2e_grams"] + result["embodied"]["co2e_grams"]
    if abs(result["co2e_grams"] - co2_parts) > max(0.000003, result["co2e_grams"] * 0.001):
        return None
    if abs(result["energy_kwh"] - result["operational"]["energy_kwh"]) > max(0.000000001, result["energy_kwh"] * 0.001):
        return None
    result["source"] = "provider_reported_estimate"
    return result


# These are the usage fields observed in supported harness stores. Raw
# usage is deliberately narrower than the source row: prompt text, tool
# arguments, and arbitrary nested metadata must never enter the durable record.
_USAGE_NUMERIC_FIELDS = frozenset({
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "total_input_tokens",
    "total_output_tokens",
    "total_tokens",
    "input",
    "output",
    "reasoning",
    "cacheRead",
    "cacheWrite",
    "totalTokens",
})
_CACHE_CREATION_FIELDS = frozenset({
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
})


def _numeric_token(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return value if math.isfinite(value) and value >= 0 else None


def _sanitize_usage(value: object) -> dict | None:
    """Keep token counters and the small amount of usage metadata we expose."""
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for key, item in value.items():
        if key in _USAGE_NUMERIC_FIELDS:
            numeric = _numeric_token(item)
            result[key] = numeric
        elif key == "cache_creation" and isinstance(item, dict):
            nested = {
                nested_key: numeric
                for nested_key, nested_value in item.items()
                if nested_key in _CACHE_CREATION_FIELDS
                and (numeric := _numeric_token(nested_value)) is not None
            }
            if nested or item:
                result[key] = {
                    nested_key: (
                        _numeric_token(nested_value)
                        if nested_key in _CACHE_CREATION_FIELDS
                        else None
                    )
                    for nested_key, nested_value in item.items()
                    if nested_key in _CACHE_CREATION_FIELDS
                }
        elif key == "output_tokens_details" and isinstance(item, dict):
            thinking = _numeric_token(item.get("thinking_tokens"))
            if "thinking_tokens" in item:
                result[key] = {"thinking_tokens": thinking}
        elif key == "cache" and isinstance(item, dict):
            result[key] = {
                nested_key: _numeric_token(nested_value)
                for nested_key, nested_value in item.items()
                if nested_key in {"read", "write"}
            }
        elif key == "iterations":
            if item is None:
                result[key] = None
            elif isinstance(item, list):
                result[key] = [_sanitize_iteration(entry) or {} for entry in item]
            else:
                # Preserve invalid structure as an invalid, content-free entry.
                # Dropping it would incorrectly make iteration coverage complete.
                result[key] = [{}]
    return result


def _sanitize_iteration(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = _sanitize_usage(value) or {}
    # These identify the billing/model variant without retaining arbitrary
    # iteration text.  All other strings are intentionally discarded.
    for key in ("type", "model"):
        if (text := _meta_text(value.get(key))) is not None:
            result[key] = text
    return result


def _iteration_layout_compatible(a: list, b: list) -> bool:
    return all(
        all(left.get(key) == right.get(key) for key in ("type", "model"))
        for left, right in zip(a, b)
    )


def _merge_sanitized_usage(existing: dict, incoming: dict) -> dict:
    """Merge counter maxima without inventing iteration roles or models.

    Retain original sanitized iteration snapshots: positions are not identities
    when a provider inserts or reorders internal calls.
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if key in ("iterations", "iteration_snapshots"):
            continue
        prior = merged.get(key)
        if isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = _merge_sanitized_usage(prior, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = max(prior, value) if isinstance(prior, (int, float)) else value
        elif prior is None:
            merged[key] = value
        elif isinstance(prior, str) and isinstance(value, str):
            merged[key] = max(prior, value)
    snapshots = []
    for raw in (existing, incoming):
        snapshots.extend(raw.get("iteration_snapshots", []))
        if "iteration_snapshots" not in raw and isinstance(raw.get("iterations"), list):
            snapshots.append(raw["iterations"])
    if snapshots:
        unique = {json.dumps(x, sort_keys=True): x for x in snapshots}
        merged["iteration_snapshots"] = [unique[k] for k in sorted(unique)]
        # Derive a deterministic representative from original snapshots only.
        # Its maxima must never re-enter the raw evidence on a later refresh.
        template = max(snapshots, key=lambda x: (len(x), json.dumps(x, sort_keys=True)))
        iterations = [{} for _ in template]
        for snapshot in merged["iteration_snapshots"]:
            if _iteration_layout_compatible(template, snapshot):
                for i, entry in enumerate(snapshot):
                    iterations[i] = _merge_sanitized_usage(iterations[i], entry)
        merged["iterations"] = iterations
    elif "iterations" in incoming and "iterations" not in merged:
        merged["iterations"] = incoming["iterations"]
    return merged


def _raw_usage_requires_record(raw_usage: dict, required: tuple = ()) -> bool:
    """Keep observations whose normalized legacy totals cannot represent them."""
    if any(key not in raw_usage for key in required):
        return True
    for key, value in raw_usage.items():
        if value is None:
            return True
        if key == "iterations" and isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and _raw_usage_requires_record(value):
            return True
    return False


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _project_name(cwd: object, fallback: str = "unknown") -> str:
    if not isinstance(cwd, str) or not cwd:
        return fallback
    path = Path(cwd)
    return path.name or str(path)


def _claude_project_fallback(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "unknown"
    return relative.parts[0] if len(relative.parts) > 1 else "unknown"


def _claude_project_id(path: Path, root: Path) -> str:
    fallback = _claude_project_fallback(path, root)
    return f"claude:{fallback}" if fallback != "unknown" else "claude:unknown"


def _paths_for(root: Path, pattern: str, start: datetime, paths: list[Path] | None) -> list[Path]:
    if paths is not None:
        return sorted(Path(path) for path in paths)
    return _recent_files(root, pattern, start)


def _read_json_lines(path: Path, *, strict: bool = False) -> Iterable[tuple[int, dict]]:
    try:
        with path.open(errors="replace") as handle:
            for line_number, raw in enumerate(handle, 1):
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    yield line_number, value
    except OSError:
        if strict:
            raise
        return


def _recent_files(root: Path, pattern: str, start: datetime) -> list[Path]:
    """Return files that could contain an event at or after start.

    A JSONL append updates mtime, so an older resumed session remains eligible.
    The event timestamp remains authoritative once the file is opened.
    """
    if not root.exists():
        return []
    threshold = start.timestamp()
    files = []
    for path in root.rglob(pattern):
        try:
            if path.stat().st_mtime >= threshold:
                files.append(path)
        except OSError:
            continue
    return sorted(files)


def _first_text(mapping: dict, *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _meta_text(*values: object, default: str | None = None, limit: int = 256) -> str | None:
    """First plain, bounded, printable string; anything else never reaches storage."""
    for value in values:
        if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable():
            return value
    return default


def _claude_tariff(usage: object) -> dict | None:
    """Pricing dimensions Claude reports per request; only bounded printable strings survive."""
    usage = usage if isinstance(usage, dict) else {}
    found = {key: text for key in ("speed", "service_tier", "inference_geo")
             if (text := _meta_text(usage.get(key), limit=64)) is not None}
    return found or None


def _merge_tariff(existing: dict | None, incoming: dict | None, latest: bool) -> dict | None:
    """Per key: the latest valid value wins, a sparse row never erases an earlier key."""
    if not incoming:
        return existing
    return {**(existing or {}), **incoming} if latest else {**incoming, **(existing or {})}


_CODEX_TIERS = {"priority": "fast", "fast": "fast", "flex": "flex", "default": "standard"}


def _codex_tariff(settings: object) -> dict | None:
    """Tariff named by a thread_settings_applied row, a full settings snapshot: Codex's "default" is explicit standard, a missing tier
    is not recorded (None, never the previous tier) and an unknown one is kept as is, so pricing leaves it unpriced instead of guessing."""
    tier = _meta_text(_mapping(settings).get("service_tier"), limit=64)
    if not tier:
        return None
    return {"service_tier": _CODEX_TIERS.get(tier.lower(), tier.lower())}


def _number(value: object) -> float | None:
    """A finite float, or None (bools, non-numbers, NaN, infinity and integers too large for a float are dropped)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


MAX_WINDOW_MINUTES = 527040  # one leap year: a longer quota window is not a real one and would only overflow date arithmetic


def _quota_window(slot: str, value: object) -> dict | None:
    window = _mapping(value)
    used, minutes = _number(window.get("used_percent")), window.get("window_minutes")
    if used is None or used < 0 or isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= MAX_WINDOW_MINUTES:
        return None
    resets, resets_at = _number(window.get("resets_at")), None
    if resets is not None and resets > 0:
        try:
            resets_at = datetime.fromtimestamp(resets, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            resets_at = None
    return {"slot": slot, "minutes": minutes, "used_percent": used, "resets_at": resets_at}


def _note_full(last_full: dict, scope: tuple, quota: dict, timestamp: datetime) -> None:
    """Remember, per full window, the time and the reset of the latest retained full evidence."""
    for w in quota.get("windows") or ():
        if (w.get("used_percent") or 0) >= 100:
            last_full[scope + (w["minutes"],)] = (timestamp, parse_iso_timestamp(w.get("resets_at")))


def _full_evidence_due(last_full: dict, scope: tuple, quota: dict, timestamp: datetime) -> bool:
    """A still-full window needs a retained reading when the last retained full evidence is older than half the window, or when its reset moved on
    by more than half the window past the old reset and the time is past that old reset (a new window instance, which the detector must see)."""
    for w in quota.get("windows") or ():
        if (w.get("used_percent") or 0) < 100:
            continue
        kept = last_full.get(scope + (w["minutes"],))
        if kept is None:
            continue
        half = timedelta(minutes=w["minutes"] / 2)
        reset = parse_iso_timestamp(w.get("resets_at"))
        if timestamp - kept[0] > half or (reset and kept[1] and timestamp >= kept[1] and reset - kept[1] > half):
            return True
    return False


def _quota_state(quota: dict) -> tuple:
    """What a quota-only transition compares: the reached type and which windows (by length) are at 100 % or more. An empty snapshot and a snapshot
    with no full window are the same state; a change in any one window's full flag is a transition."""
    return (quota.get("reached"), frozenset(w.get("minutes") for w in quota.get("windows") or () if (w.get("used_percent") or 0) >= 100))


def _codex_quota(value: object) -> dict | None:
    """Compact quota snapshot from a token_count rate_limits object: limit windows and plan only. The credit balance, limit name
    and other account state are never kept. None when there is neither a valid window, a reached-limit type, a limit id nor a plan."""
    limits = _mapping(value)
    windows = [w for slot in ("primary", "secondary") if (w := _quota_window(slot, limits.get(slot))) is not None]
    reached = _meta_text(limits.get("rate_limit_reached_type"), limit=64)
    limit_id, plan_type = _meta_text(limits.get("limit_id"), limit=64), _meta_text(limits.get("plan_type"), limit=64)
    if not windows and reached is None and limit_id is None and plan_type is None:
        return None
    # A present snapshot with no window and no reached type is kept (empty windows): it is the evidence that a limit has recovered.
    return {"limit_id": limit_id, "plan_type": plan_type, "reached": reached, "windows": windows}


_CLAUDE_LIMIT_MINUTES = {"five_hour": 300, "seven_day": 10080}


def _claude_quota(value: object) -> dict | None:
    """Quota object for a rejected request (quotaLimits.status == "rejected"); None for anything else."""
    limits = _mapping(value)
    if limits.get("status") != "rejected":
        return None
    kind = _meta_text(limits.get("rateLimitType"), limit=64)
    resets = _number(limits.get("resetsAt"))
    resets_at = None
    if resets is not None and resets > 0:
        try:
            resets_at = datetime.fromtimestamp(resets, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            resets_at = None
    windows = []
    if kind in _CLAUDE_LIMIT_MINUTES:
        windows.append({"slot": kind, "minutes": _CLAUDE_LIMIT_MINUTES[kind], "used_percent": 100.0, "resets_at": resets_at})
    return {"limit_id": None, "plan_type": None, "reached": kind, "status": "rejected", "resets_at": resets_at, "windows": windows}


def _explicit_turn_id(row: dict, message: dict) -> str | None:
    return _first_text(
        row,
        "turn_id", "turnId",
    ) or _first_text(message, "turn_id", "turnId")


_SYSTEM_REMINDERS_ONLY = re.compile(r"\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.S)


def _claude_injected_only(content) -> bool:
    """True when a Claude user row's text is only an interruption marker or system reminders: Claude Code wrote it, the user did not."""
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        if any(isinstance(i, dict) and i.get("type") in {"tool_result", "tool_use_result", "image"} for i in content):
            return False
        texts = [i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"]
        if not texts:
            return False
    else:
        return False
    joined = "\n".join(t for t in texts if isinstance(t, str)).strip()
    return bool(joined) and (joined.startswith("[Request interrupted by user") or bool(_SYSTEM_REMINDERS_ONLY.fullmatch(joined)))


INTERRUPTED = "interrupted"


_CLAUDE_INTERRUPT_MARKERS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})


def _claude_interrupt_marker(row: dict) -> bool:
    """True for the user row Claude Code writes when the user stops a request ("[Request interrupted by user]", or "... for tool use")."""
    if row.get("type") != "user":
        return False
    content = _mapping(row.get("message")).get("content", row.get("content"))
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        if any(not isinstance(i, dict) or i.get("type") != "text" for i in content):
            return False  # an image, tool result or other block next to the text: a real message, not the marker
        texts = [i.get("text", "") for i in content]
    else:
        return False
    found = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
    return bool(found) and all(t in _CLAUDE_INTERRUPT_MARKERS for t in found)  # marker-only: a message quoting it is a real prompt


def _flag_list(keys: set, key) -> list | None:
    return [INTERRUPTED] if key in keys else None


def _is_genuine_user_row(row: dict) -> bool:
    if row.get("type") not in {"user", "user_message"} and _mapping(row.get("message")).get("role") != "user":
        return False
    if row.get("type") == "user" and (row.get("isMeta") is True or row.get("isCompactSummary") is True):
        return False  # Claude Code injected text (skill bodies, local-command caveats, compaction summaries), not typed by the user
    message = _mapping(row.get("message"))
    content = message.get("content", row.get("content"))
    if row.get("type") == "user" and _claude_injected_only(content):
        return False
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return True
    return any(
        not isinstance(item, dict) or item.get("type") not in {"tool_result", "tool_use_result"}
        for item in content
    )


def _claude_cache_write(usage: dict) -> int:
    if counter := usage.get("cache_creation_input_tokens"):
        return _nonnegative_int(counter)
    if usage.get("cache_creation_input_tokens") == 0:
        return 0
    split = _mapping(usage.get("cache_creation"))
    return sum(_nonnegative_int(split.get(key)) for key in _CACHE_CREATION_FIELDS)


def collect_claude(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Claude calls, deduplicating streamed rows by requestId.

    Claude can write several content-block rows for a request.  Early rows may
    contain placeholder output counts, so every token field uses its maximum.
    """
    calls: dict[str, dict] = {}
    stops: list[list[str]] = []
    for path in _paths_for(root, "*.jsonl", start, paths):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = path.parts
        is_subagent_path = "subagents" in parts
        if is_subagent_path and path.name == "journal.jsonl":
            continue
        subagent_index = parts.index("subagents") if is_subagent_path else 0
        path_session = _meta_text(parts[subagent_index - 1] if subagent_index else None) \
            if is_subagent_path else None
        fallback_session = path_session or _meta_text(path.stem, default="unknown")
        fallback_project = _claude_project_fallback(path, root)
        cowork = path.is_relative_to(COWORK_SESSIONS)
        last_user_turn: str | None = None
        turn_requests: list[str] = []  # request keys of the current turn in this file, in order
        requests_turn: str | None = None  # the explicit turn id those requests carry, if any
        for line_number, row in _read_json_lines(path, strict=strict):
            message = _mapping(row.get("message"))
            usage = message.get("usage")
            timestamp = parse_iso_timestamp(row.get("timestamp"))
            row_uuid = _first_text(row, "uuid", "id")
            # Main-thread transcripts rarely carry the marker; a stopped subagent's file does, and its flag rolls up to the parent turn.
            marker_turn = _meta_text(_explicit_turn_id(row, message))
            if turn_requests and _claude_interrupt_marker(row) and (marker_turn is None or marker_turn == requests_turn):
                stops.append(list(turn_requests))  # resolved after eligibility: the last request kept is the one running
            if _is_genuine_user_row(row):
                last_user_turn = _meta_text(row_uuid) or _stable_hash({
                    "session": row.get("sessionId"),
                    "timestamp": row.get("timestamp"),
                    "line": line_number,
                })
                # The effective turn of the requests that follow (derived from this row unless they name one), for matching markers.
                turn_requests, requests_turn = [], marker_turn or last_user_turn
            quota = _claude_quota(row.get("quotaLimits"))
            if quota is not None and not isinstance(usage, dict):
                usage = {}
            if not isinstance(usage, dict) or timestamp is None:
                continue
            request_id = _first_text(row, "requestId")
            session_id = _meta_text(row.get("sessionId"), default=fallback_session)
            id_synthetic = False
            if not request_id:
                request_id = _first_text(message, "id")
            if not request_id:
                request_id = row_uuid
                id_synthetic = True
            if not request_id:
                request_id = _stable_hash({
                    "session_id": session_id,
                    "timestamp": row.get("timestamp"),
                    "message": {
                        key: message.get(key)
                        for key in ("id", "model", "role")
                        if message.get(key) is not None
                    },
                    "usage": _sanitize_usage(usage),
                })
                id_synthetic = True
            key = str(request_id)
            row_turn = _meta_text(_explicit_turn_id(row, message))
            if row_turn is not None and row_turn != requests_turn:
                turn_requests, requests_turn = [], row_turn  # a new explicit turn: the earlier turn's requests are no candidates
            if not turn_requests or turn_requests[-1] != key:
                turn_requests.append(key)
            values = {
                "fresh_input": _nonnegative_int(usage.get("input_tokens")),
                "cache_read": _nonnegative_int(usage.get("cache_read_input_tokens")),
                "cache_write": _claude_cache_write(usage),
                "output": _nonnegative_int(usage.get("output_tokens")),
                "reasoning": _nonnegative_int(
                    _mapping(usage.get("output_tokens_details")).get("thinking_tokens")
                ),
                "raw_usage": _sanitize_usage(usage) or {},
            }
            parent_session_id = _meta_text(
                row.get("parentSessionId"), row.get("parent_session_id"), path_session)
            stop = message.get("stop_reason")
            has_stop = isinstance(stop, str) and bool(stop)
            existing = calls.get(key)
            if existing is None:
                existing = {**values, "output_final": has_stop, "timestamp": timestamp, "_first": timestamp,
                            "session_id": session_id, "parent_session_id": parent_session_id,
                            "_fallback": (fallback_project, _claude_project_id(path, root)),
                            "_cowork": cowork}
                calls[key] = existing
            else:
                for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                    existing[token_field] = max(existing[token_field], values[token_field])
                existing["raw_usage"] = _merge_sanitized_usage(
                    existing.get("raw_usage", {}), values["raw_usage"]
                )
                existing["timestamp"] = max(existing["timestamp"], timestamp)
                existing["output_final"] = existing["output_final"] or has_stop
                # A copied request belongs to the session of its earliest row; equal
                # timestamps keep the first one seen, independent of session ids.
                if timestamp < existing["_first"]:
                    existing.update(_first=timestamp, session_id=session_id,
                                    parent_session_id=parent_session_id)

            # Merge dimensions per field: the latest valid value wins, but a sparse
            # streaming row never erases what an earlier row established.
            agent_id = row.get("agentId")
            explicit_turn = _meta_text(_explicit_turn_id(row, message))
            derived_turn = None if is_subagent_path else last_user_turn
            cwd_value = _meta_text(row.get("cwd"), limit=4096)
            turn = explicit_turn or derived_turn
            latest = timestamp >= existing["timestamp"]
            for name, value in {
                "model": None if quota is not None else _meta_text(message.get("model")),
                "effort": _meta_text(row.get("effort")),
                "quota": quota,
                "project": _project_name(cwd_value) if cwd_value else None,
                "project_id": cwd_value,
                "cwd": cwd_value,
                "turn_id": turn,
                "turn_confidence": None if turn is None else "observed" if explicit_turn else "derived",
                "harness_version": _meta_text(row.get("version"), message.get("version")),
                "entrypoint": _meta_text(row.get("entrypoint")),
                "thread_kind": "subagent" if agent_id or is_subagent_path else None,
                "agent": _meta_text(row.get("attributionAgent"), agent_id),
            }.items():
                if value is not None and (latest or existing.get(name) is None):
                    existing[name] = value
            if (tariff := _claude_tariff(usage)) is not None:
                existing["tariff"] = _merge_tariff(existing.get("tariff"), tariff, latest)
            if latest or "id_synthetic" not in existing:
                existing["id_synthetic"] = id_synthetic

    def eligible(values):
        return not (sum(
            values[field]
            for field in ("fresh_input", "cache_read", "cache_write", "output")
        ) <= 0 and not _raw_usage_requires_record(values.get("raw_usage", {}),
            ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')))
    # The stopped request is chosen among usage-eligible requests before the time window, so the window never changes which one carries the flag.
    usable = {call_id for call_id, values in calls.items() if eligible(values)}
    # A rejected request (quota limit event) has no tokens but is kept; it is never the stopped request.
    retained = {call_id for call_id, values in calls.items() if (call_id in usable or values.get("quota") is not None)
                and start <= values["timestamp"] < end}
    interrupted = {next(k for k in reversed(keys) if k in usable) for keys in stops if any(k in usable for k in keys)}
    records = []
    for call_id, values in calls.items():
        if call_id not in retained:
            continue
        fallback_project, fallback_id = values.pop("_fallback")
        if values.pop("_cowork"):
            values.setdefault("entrypoint", "local-agent")  # Cowork rows often carry no entrypoint
        del values["_first"]
        for name, default in (
            ("model", "unknown"), ("effort", "unknown"), ("entrypoint", "unknown"),
            ("agent", "main"), ("thread_kind", "main"), ("cwd", None), ("turn_id", None),
            ("turn_confidence", "absent"), ("harness_version", None), ("tariff", None), ("quota", None),
            ("project", fallback_project), ("project_id", fallback_id),
        ):
            values.setdefault(name, default)
        values["flags"] = _flag_list(interrupted, call_id)
        records.append(
            AttributionRecord(
                harness="claude", provider="anthropic", call_id=call_id, **values
            )
        )
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def _codex_thread(source: object, thread_source: object) -> tuple[str, str, str | None]:
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        subagent = source["subagent"]
        spawn = _mapping(subagent.get("thread_spawn"))
        agent = _meta_text(spawn.get("agent_nickname"), spawn.get("agent_role"),
                           subagent.get("other"), default="subagent")
        return "subagent", agent, _meta_text(spawn.get("parent_thread_id"))
    source_name = _meta_text(thread_source, source, default="main")
    if source_name in {"subagent", "automation"}:
        return source_name, source_name, None
    return "main", "main", None


def _codex_event_turn_id(row: dict, payload: dict) -> str | None:
    for mapping in (payload, row):
        found = _first_text(mapping, "turn_id", "turnId")
        if found:
            return found
    return None


def _codex_user_event_identity(row: dict, payload: dict) -> str | None:
    for mapping in (payload, row):
        found = _first_text(mapping, "event_id", "eventId", "id", "uuid")
        if found:
            return found
    return None


_CODEX_TURN_END = frozenset({"task_complete", "turn_complete", "turn_aborted"})


def collect_codex(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Codex per-turn deltas from rollout JSONL files.

    A rollout without a usable session ID gets a deterministic synthetic ID
    from its timestamp, source ordinal, and sanitized counters.  Such an
    identity is stable for copied files but remains inherently ambiguous when
    independent sessions emit identical events.
    """
    calls: dict[tuple[str, str], dict] = {}
    interrupted: set[tuple[str, str]] = set()
    for path in _paths_for(root, "rollout-*.jsonl", start, paths):
        rows = list(_read_json_lines(path, strict=strict))
        meta_payload = next(
            (_mapping(row.get("payload")) for _, row in rows if row.get("type") == "session_meta"),
            {},
        )
        session_id_value = _first_text(meta_payload, "id")
        session_id_value = _meta_text(session_id_value)
        session_id = session_id_value or "unknown"
        has_session_meta = session_id_value is not None
        provider = _meta_text(meta_payload.get("model_provider"), default="openai")
        originator = "unknown"
        source: object = meta_payload.get("source") or "unknown"
        thread_source: object = meta_payload.get("thread_source")
        cwd: object = _meta_text(meta_payload.get("cwd"), limit=4096)
        model = "unknown"
        effort = "unknown"
        current_turn_id: str | None = None
        turn_confidence = "absent"
        pending_turn_id: str | None = None
        seen_explicit = False  # once a file carries explicit turn ids, derived user events never move the turn
        last_total_signature = None
        counter_segment = 0
        tariff: dict | None = None  # latest thread_settings.service_tier in this file, until it changes
        last_call: tuple[str, str] | None = None  # latest record of the current turn, flagged by a turn_aborted event
        last_quota: dict = {}  # (limit id, plan) -> (reached type, windows at 100 %) of the latest quota seen in this file, for quota-only transitions
        pending_full: dict = {}  # (limit id, plan, window minutes) -> the latest full quota-only reading seen but not retained
        last_full: dict = {}  # (limit id, plan, window minutes) -> (time, reset) of the latest retained full evidence in this file, for checkpoint events
        harness_version = _first_text(meta_payload, "cli_version", "version")
        originator = _meta_text(meta_payload.get("originator"), source, thread_source, default=originator)

        for line_number, row in rows:
            payload = _mapping(row.get("payload"))
            row_type = row.get("type")
            if row_type == "session_meta":
                session_id = _meta_text(payload.get("id"), default=session_id)
                provider = _meta_text(payload.get("model_provider"), default=provider)
                source = payload.get("source") or source
                thread_source = payload.get("thread_source") or thread_source
                originator = _meta_text(payload.get("originator"), source, thread_source, default=originator)
                cwd = _meta_text(payload.get("cwd"), default=cwd, limit=4096)
                harness_version = _first_text(payload, "cli_version", "version") or harness_version
                continue
            if row_type == "turn_context":
                model = _meta_text(payload.get("model"), default="unknown")
                effort = _meta_text(payload.get("effort"), payload.get("reasoning_effort"), default="unknown")
                cwd = _meta_text(payload.get("cwd"), default=cwd, limit=4096)
                explicit_turn = _meta_text(_codex_event_turn_id(row, payload))
                if explicit_turn:
                    current_turn_id, turn_confidence, seen_explicit = explicit_turn, "observed", True
                elif not seen_explicit:
                    current_turn_id = pending_turn_id
                    turn_confidence = "derived" if pending_turn_id else "absent"
                pending_turn_id = None
                continue
            if row_type == "event_msg" and payload.get("type") == "thread_settings_applied":
                tariff = _codex_tariff(payload.get("thread_settings"))
                continue
            explicit_turn = _meta_text(_codex_event_turn_id(row, payload))
            if explicit_turn:  # task_started, item_completed, token_usage_record, ... name their turn
                current_turn_id, turn_confidence, seen_explicit = explicit_turn, "observed", True
                pending_turn_id = explicit_turn
            if row_type != "event_msg" or payload.get("type") != "token_count":
                event_type = payload.get("type")
                if row_type == "event_msg" and event_type in _CODEX_TURN_END:
                    if event_type == "turn_aborted" and last_call is not None \
                            and calls[last_call]["turn_id"] == current_turn_id:
                        interrupted.add(last_call)
                    last_call = None
                    seen_explicit = False  # explicitness is per turn: a legacy-style turn may follow in the same file
                    continue
                if explicit_turn or seen_explicit:
                    continue
                if event_type == "task_started":
                    last_call = None
                    current_turn_id = pending_turn_id = None
                    turn_confidence = "absent"
                elif (row_type == "event_msg" and event_type in {"user_message", "user_input"}) or (
                        row_type == "response_item" and event_type == "message" and payload.get("role") == "user"):
                    last_call = None
                    current_turn_id = pending_turn_id = _meta_text(_codex_user_event_identity(row, payload))
                    turn_confidence = "derived" if current_turn_id else "absent"
                continue
            info = _mapping(payload.get("info"))
            usage = info.get("last_token_usage")
            timestamp = parse_iso_timestamp(row.get("timestamp"))

            def quota_event() -> None:
                """A token_count that produces no usage record (quota-only, repeated counters, zero tokens) still reports the account's limits: keep a
                zero-token quota event when they changed since the last quota in this file (reached type, or a window crossing 100 %), so the moment a
                limit is reached (the requests then fail and carry no usage) and its recovery are not lost.

                A still-full window also needs retained evidence at least every window length while readings continue, so the detector sees an
                expiry only across a real gap. The last full reading seen but not retained is remembered; when a new full reading arrives more than
                half a window after the last retained evidence, the remembered one (the reading just before the gap) is retained first."""
                quota = _codex_quota(payload.get("rate_limits"))
                if quota is None or timestamp is None:
                    return
                scope = (quota.get("limit_id"), quota.get("plan_type"))
                state = _quota_state(quota)
                thread_kind, agent, parent_session_id = _codex_thread(source, thread_source)
                ordinal = row.get("ordinal")
                identity = ordinal if ordinal is not None else _stable_hash({"timestamp": row.get("timestamp"), "quota": quota})
                if has_session_meta:
                    call_id, synthetic = f"{session_id}:{identity}:quota", ordinal is None
                else:
                    call_id, synthetic = "synthetic:" + _stable_hash({"timestamp": row.get("timestamp"), "ordinal": ordinal, "quota": quota}), True
                this = ((provider, call_id), {
                    "timestamp": timestamp, "session_id": session_id, "call_id": call_id, "model": "unknown", "effort": "unknown",
                    "project": _project_name(cwd), "project_id": str(cwd) if isinstance(cwd, str) and cwd else "unknown",
                    "cwd": cwd if isinstance(cwd, str) else None, "turn_id": current_turn_id, "turn_confidence": turn_confidence,
                    "parent_session_id": parent_session_id, "harness_version": harness_version, "entrypoint": originator,
                    "thread_kind": thread_kind, "agent": agent, "fresh_input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0,
                    "raw_usage": {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0,
                                  "reasoning_output_tokens": 0},
                    "id_synthetic": synthetic, "tariff": None, "quota": {**quota, "status": "event"}}, quota)
                fulls = [scope + (w["minutes"],) for w in quota.get("windows") or () if (w.get("used_percent") or 0) >= 100]

                def retain(item) -> None:
                    calls.setdefault(item[0], item[1])
                    _note_full(last_full, scope, item[2], item[1]["timestamp"])
                    for k in fulls:
                        pending_full.pop(k, None)

                for k in fulls:  # the reading just before a gap is retained before the new one is judged
                    kept, seen = last_full.get(k), pending_full.get(k)
                    if kept and seen and timestamp - kept[0] > timedelta(minutes=k[2] / 2) and seen[1]["timestamp"] > kept[0]:
                        calls.setdefault(seen[0], seen[1])
                        _note_full(last_full, scope, seen[2], seen[1]["timestamp"])
                if state != last_quota.get(scope, (None, frozenset())) or _full_evidence_due(last_full, scope, quota, timestamp):
                    last_quota[scope] = state
                    retain(this)
                else:
                    for k in fulls:
                        pending_full[k] = this

            if not isinstance(usage, dict) or timestamp is None:
                quota_event()
                continue
            cumulative = _mapping(info.get("total_token_usage"))
            if cumulative:
                signature = tuple(
                    _nonnegative_int(cumulative.get(field)) for field in (
                        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "output_tokens", "reasoning_output_tokens", "total_tokens",
                    )
                )
                if signature == last_total_signature:
                    quota_event()
                    continue
                if last_total_signature is not None and any(
                    new < old for new, old in zip(signature, last_total_signature)
                ):
                    counter_segment += 1
                last_total_signature = signature
            total_input = _nonnegative_int(usage.get("input_tokens"))
            cache_read = _nonnegative_int(usage.get("cached_input_tokens"))
            cache_write = _nonnegative_int(usage.get("cache_write_input_tokens"))
            output = _nonnegative_int(usage.get("output_tokens"))
            reasoning = _nonnegative_int(usage.get("reasoning_output_tokens"))
            raw_usage = _sanitize_usage(usage) or {}
            if (
                total_input + cache_read + cache_write + output + reasoning <= 0
                and not _raw_usage_requires_record(raw_usage,
                    ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens'))
            ):
                quota_event()
                continue
            thread_kind, agent, parent_session_id = _codex_thread(source, thread_source)
            ordinal = row.get("ordinal")
            if has_session_meta:
                event_identity = ordinal if ordinal is not None else _stable_hash({
                    "timestamp": row.get("timestamp"),
                    "total": _sanitize_usage(cumulative) if cumulative else raw_usage,
                    "segment": counter_segment,
                })
                call_id = f"{session_id}:{event_identity}:token_count"
                id_synthetic = ordinal is None and not cumulative
            else:
                call_id = "synthetic:" + _stable_hash({
                    "timestamp": row.get("timestamp"),
                    "ordinal": ordinal,
                    "segment": counter_segment,
                    "usage": _sanitize_usage(usage),
                    "total": _sanitize_usage(info.get("total_token_usage")),
                })
                id_synthetic = True
            key = (provider, call_id)
            candidate = {
                "timestamp": timestamp,
                "session_id": session_id,
                "call_id": call_id,
                "model": model,
                "effort": effort,
                "project": _project_name(cwd),
                "project_id": str(cwd) if isinstance(cwd, str) and cwd else "unknown",
                "cwd": cwd if isinstance(cwd, str) else None,
                "turn_id": current_turn_id,
                "turn_confidence": turn_confidence,
                "parent_session_id": parent_session_id,
                "harness_version": harness_version,
                "entrypoint": originator,
                "thread_kind": thread_kind,
                "agent": agent,
                "fresh_input": max(0, total_input - cache_read - cache_write),
                "cache_read": cache_read,
                "cache_write": cache_write,
                "output": output,
                "reasoning": reasoning,
                "raw_usage": raw_usage,
                "id_synthetic": id_synthetic,
                "tariff": tariff,
                "quota": _codex_quota(payload.get("rate_limits")),
            }
            if candidate["quota"] is not None:
                scope = (candidate["quota"].get("limit_id"), candidate["quota"].get("plan_type"))
                last_quota[scope] = _quota_state(candidate["quota"])
                _note_full(last_full, scope, candidate["quota"], timestamp)
                for w in candidate["quota"].get("windows") or ():
                    pending_full.pop(scope + (w["minutes"],), None)
            existing = calls.get(key)
            if existing is None:
                calls[key] = candidate
            else:
                for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                    existing[token_field] = max(existing[token_field], candidate[token_field])
                existing["raw_usage"] = _merge_sanitized_usage(
                    existing.get("raw_usage", {}), raw_usage
                )
                existing["tariff"] = _merge_tariff(existing.get("tariff"), tariff, timestamp >= existing["timestamp"])
                existing["quota"] = candidate["quota"] or existing.get("quota") if timestamp >= existing["timestamp"] \
                    else existing.get("quota") or candidate["quota"]
                if timestamp >= existing["timestamp"]:
                    existing.update({
                        field: candidate[field]
                        for field in (
                            "timestamp", "model", "effort", "project", "project_id", "cwd",
                            "turn_id", "turn_confidence", "parent_session_id", "harness_version",
                            "entrypoint", "thread_kind", "agent",
                        )
                    })
            last_call = key
    records = [
        AttributionRecord(harness="codex", provider=provider, **values, flags=_flag_list(interrupted, (provider, values["call_id"])))
        for (provider, _), values in calls.items()
        if start <= values["timestamp"] < end
    ]
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def collect_pi(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Pi response usage and deduplicate copied fork history."""
    calls: dict[tuple[str, str], dict] = {}
    interrupted: set[tuple[str, str]] = set()
    for path in _paths_for(root, "*.jsonl", start, paths):
        rows = list(_read_json_lines(path, strict=strict))
        session = next((row for _, row in rows if row.get("type") == "session"), {})
        session_id = _meta_text(session.get("id"), path.stem, default="unknown")
        session_started = parse_iso_timestamp(session.get("timestamp"))
        cwd = _meta_text(session.get("cwd"), limit=4096)
        version = session.get("version")
        harness_version = str(version) if isinstance(version, int) and not isinstance(version, bool) \
            else _meta_text(version, limit=64)
        last_user_turn: str | None = None
        last_call: tuple[str, str] | None = None  # latest record of the current turn
        for _, row in rows:
            message = _mapping(row.get("message"))
            usage = message.get("usage")
            if row.get("type") == "message" and _is_genuine_user_row(row):
                last_user_turn = _meta_text(row.get("id"))
                last_call = None
            is_assistant = row.get("type") == "message" and message.get("role") == "assistant"
            aborted = is_assistant and message.get("stopReason") == "aborted"
            if not is_assistant or not isinstance(usage, dict):
                if aborted and last_call is not None:  # stopped before any usage was written: the turn's last request was running
                    interrupted.add(last_call)
                continue
            timestamp = parse_iso_timestamp(row.get("timestamp") or message.get("timestamp"))
            if timestamp is None:
                continue
            raw_usage = _sanitize_usage(usage) or {}
            provider = _meta_text(message.get("provider"), default="unknown")
            environment = clean_environment(usage.get("emissions"), provider)
            response_id = _first_text(message, "responseId", "response_id")
            id_synthetic = response_id is None
            call_id = response_id or "synthetic:" + _stable_hash({
                "entry_id": row.get("id"), "timestamp": timestamp.isoformat(),
                "provider": provider, "model": message.get("model"), "usage": raw_usage,
            })
            values = {
                "fresh_input": _nonnegative_int(usage.get("input")),
                "cache_read": _nonnegative_int(usage.get("cacheRead")),
                "cache_write": _nonnegative_int(usage.get("cacheWrite")),
                "output": _nonnegative_int(usage.get("output")),
                "reasoning": _nonnegative_int(usage.get("reasoning")),
            }
            if sum(values[field] for field in ("fresh_input", "cache_read", "cache_write", "output")) <= 0 \
                    and not _raw_usage_requires_record(raw_usage, ("input", "cacheRead", "cacheWrite", "output")):
                if aborted and last_call is not None:
                    interrupted.add(last_call)
                continue
            candidate = {
                "timestamp": timestamp, "session_id": session_id, "call_id": call_id,
                "model": _meta_text(message.get("model"), default="unknown"), "effort": "unknown",
                "project": _project_name(cwd), "project_id": cwd or "unknown", "cwd": cwd,
                "turn_id": last_user_turn, "turn_confidence": "derived" if last_user_turn else "absent",
                "parent_session_id": None, "harness_version": harness_version, "entrypoint": "unknown",
                "thread_kind": "main", "agent": "main", "raw_usage": raw_usage,
                "id_synthetic": id_synthetic,
                "environment": environment,
                "session_started": session_started or timestamp,
                **values,
            }
            key = (provider, call_id)
            last_call = key
            if aborted:
                interrupted.add(key)
            existing = calls.get(key)
            if existing is None:
                calls[key] = candidate
                continue
            for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                existing[token_field] = max(existing[token_field], candidate[token_field])
            existing["raw_usage"] = _merge_sanitized_usage(existing["raw_usage"], raw_usage)
            if existing.get("environment") is None and environment is not None:
                existing["environment"] = environment
            existing["timestamp"] = max(existing["timestamp"], timestamp)
            if candidate["session_started"] < existing["session_started"]:
                for field in ("session_id", "project", "project_id", "cwd", "harness_version", "session_started"):
                    existing[field] = candidate[field]
    records = []
    for (provider, _), values in calls.items():
        values["session_started_at"] = values.pop("session_started")
        if start <= values["timestamp"] < end:
            records.append(AttributionRecord(harness="pi", provider=provider, flags=_flag_list(interrupted, (provider, values["call_id"])), **values))
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def _millisecond_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def collect_opencode(
    db_path: Path,
    start: datetime,
    end: datetime,
    diagnostics: dict[str, int] | None = None,
) -> list[AttributionRecord]:
    """Collect per-assistant-call usage from OpenCode's local SQLite store."""
    path = Path(db_path).expanduser().resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("""
            SELECT m.id, m.session_id, m.time_created, m.time_updated, m.data,
                   s.parent_id, s.directory, s.version
            FROM message AS m JOIN session AS s ON s.id = m.session_id
        """).fetchall()
    finally:
        connection.close()
    records = []
    anchors: list[tuple[str, str | None, datetime, str]] = []  # every request with usage, in or out of the window
    aborts: list[tuple[str, str | None, str | None, datetime | None]] = []  # (session, turn, own call id): an abort usually carries no tokens, so it is read before the token guards
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            if diagnostics is not None:
                diagnostics["malformed_lines"] += 1
            continue
        if not isinstance(data, dict):
            if diagnostics is not None:
                diagnostics["malformed_lines"] += 1
            continue
        if data.get("role") != "assistant":
            continue
        if _mapping(data.get("error")).get("name") == "MessageAbortedError":
            aborts.append((_meta_text(row["session_id"], default="unknown"), _meta_text(_first_text(data, "parentID", "parentId")),
                           str(row["id"]) if row["id"] else None, _millisecond_timestamp(
                               _mapping(data.get("time")).get("completed") or _mapping(data.get("time")).get("created")
                               or row["time_updated"] or row["time_created"])))
        usage = data.get("tokens")
        if not isinstance(usage, dict):
            if diagnostics is not None:
                diagnostics["unparsed_usage_lines"] += 1
            continue
        time_data = _mapping(data.get("time"))
        timestamp = _millisecond_timestamp(
            time_data.get("completed") or time_data.get("created") or row["time_updated"] or row["time_created"]
        )
        if timestamp is None:
            if diagnostics is not None:
                diagnostics["unparsed_usage_lines"] += 1
            continue
        in_window = start <= timestamp < end  # requests outside the window still anchor an abort, so fresh and incremental reads agree
        raw_usage = _sanitize_usage(usage) or {}
        cache = _mapping(usage.get("cache"))
        values = {
            "fresh_input": _nonnegative_int(usage.get("input")),
            "cache_read": _nonnegative_int(cache.get("read")),
            "cache_write": _nonnegative_int(cache.get("write")),
            # OpenCode's own stats add reasoning to output; normalize to the
            # repository convention where reasoning is a subset of output.
            "output": _nonnegative_int(usage.get("output")) + _nonnegative_int(usage.get("reasoning")),
            "reasoning": _nonnegative_int(usage.get("reasoning")),
        }
        if sum(values[field] for field in ("fresh_input", "cache_read", "cache_write", "output")) <= 0 \
                and not _raw_usage_requires_record(raw_usage, ("input", "output", "reasoning", "cache")):
            continue
        call_id = str(row["id"] or "synthetic:" + _stable_hash({
            "session": row["session_id"], "timestamp": timestamp.isoformat(), "usage": raw_usage,
        }))
        cwd = _meta_text(_mapping(data.get("path")).get("cwd"), row["directory"], limit=4096)
        parent = _meta_text(row["parent_id"])
        turn_id = _meta_text(_first_text(data, "parentID", "parentId"))
        anchors.append((_meta_text(row["session_id"], default="unknown"), turn_id, timestamp, call_id))
        if not in_window:
            continue
        records.append(AttributionRecord(
            harness="opencode", provider=_meta_text(data.get("providerID"), default="unknown"),
            timestamp=timestamp, session_id=_meta_text(row["session_id"], default="unknown"), call_id=call_id,
            model=_meta_text(data.get("modelID"), default="unknown"),
            effort=_meta_text(data.get("variant"), default="unknown"),
            project=_project_name(cwd), project_id=cwd or "unknown", cwd=cwd,
            entrypoint="unknown", thread_kind="subagent" if parent else "main",
            agent=_meta_text(data.get("agent"), default="unknown"), turn_id=turn_id,
            turn_confidence="observed" if turn_id else "absent", parent_session_id=parent,
            harness_version=str(row["version"]) if row["version"] is not None else None,
            raw_usage=raw_usage, id_synthetic=not bool(row["id"]),
            **values,
        ))
    if aborts:
        by_id = {r.call_id: i for i, r in enumerate(records)}
        flagged: set[int] = set()
        for session, turn, own, at in aborts:
            if own in by_id:  # the aborted message itself carries usage
                flagged.add(by_id[own])
                continue
            same = [a for a in anchors if turn and at and a[0] == session and a[1] == turn and a[2] <= at]
            if same:  # the latest request of the turn up to the abort was the one running; it counts only if this read retains it
                target = max(same, key=lambda a: (a[2], a[3]))[3]
                if target in by_id:
                    flagged.add(by_id[target])
        records = [replace(r, flags=[INTERRUPTED]) if i in flagged else r for i, r in enumerate(records)]
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def reconcile_codex_rollout(path: Path) -> dict:
    """Compare deltas with cumulative totals, accounting for counter resets."""
    fields = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens",
    )
    summed = {field: 0 for field in fields}
    final_snapshot = None
    completed_segments = {field: 0 for field in fields}
    segments = 0
    calls = 0
    last_total_signature = None
    for _, row in _read_json_lines(path):
        payload = _mapping(row.get("payload"))
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = _mapping(payload.get("info"))
        last = info.get("last_token_usage")
        total = _mapping(info.get("total_token_usage"))
        if total:
            normalized_total = {
                field: _nonnegative_int(total.get(field)) for field in fields
            }
            signature = tuple(
                _nonnegative_int(total.get(field))
                for field in (*fields, "total_tokens")
            )
            if signature == last_total_signature:
                continue
            if final_snapshot is not None and any(
                normalized_total[field] < final_snapshot[field] for field in fields
            ):
                for field in fields:
                    completed_segments[field] += final_snapshot[field]
                segments += 1
            last_total_signature = signature
        if isinstance(last, dict):
            calls += 1
            for field in fields:
                summed[field] += _nonnegative_int(last.get(field))
        if total:
            final_snapshot = normalized_total
    cumulative = None
    if final_snapshot is not None:
        cumulative = {
            field: completed_segments[field] + final_snapshot[field] for field in fields
        }
        segments += 1
    differences = {
        field: summed[field] - cumulative[field] for field in fields
    } if cumulative is not None else None
    return {
        "calls": calls,
        "summed": summed,
        "final": cumulative,
        "final_snapshot": final_snapshot,
        "segments": segments,
        "differences": differences,
        "matches": differences is not None and all(value == 0 for value in differences.values()),
    }
