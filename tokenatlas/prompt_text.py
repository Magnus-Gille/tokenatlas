"""Opt-in prompt previews: read the user prompt behind a turn id from the local source log.

Only reading and sanitizing live here; storage and integration are elsewhere.
Nothing in this module raises for bad input: unreadable or unknown -> None.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .provenance import local_file

from .why import (
    _CODEX_TURN_END, _codex_event_turn_id, _codex_user_event_identity, _explicit_turn_id,
    _first_text, _is_genuine_user_row, _mapping, _meta_text,
)

REDACTED = "[redacted]"
_SECRET_PATTERNS = [re.compile(p, flags) for p, flags in (
    (r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S),
    (r"\bsk-[A-Za-z0-9_-]{8,}", 0),
    (r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})", 0),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", 0),
    (r"\bxox[abprs]-[A-Za-z0-9-]{8,}", 0),
    (r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", 0),
    (r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{10,}", 0),
    (r"\bwhsec_[A-Za-z0-9]{10,}", 0),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", 0),
)]
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{12,}=*")
_AUTHORIZATION = re.compile(r"""(?i)\b(authorization["']?\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?("[^"]*"|'[^']*'|\S+)""")
_KEY_VALUE = re.compile(
    r"""(?i)(\w*(?:password|passwd|secret|token|api[_-]?key)\w*["']?\s*[=:]\s*)("[^"]*"|'[^']*'|\S+)""")
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
# base64-ish run: >=40 chars, must mix letters and digits so long plain words/paths are spared
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/_=-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{40,}={0,2}")
_MARKER_PARTIAL = re.compile(r"\[(?:r(?:e(?:d(?:a(?:c(?:t(?:e(?:d)?)?)?)?)?)?)?)?$")
_WRAPPERS = re.compile(
    r"<(system-reminder|command-message|command-args|local-command-stdout|local-command-stderr)\b[^>]*>.*?</\1>",
    re.S)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)


def sanitize(text: object, limit: int = 200) -> str | None:
    """Collapse whitespace, mask secrets, then truncate at a word boundary with an ellipsis."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    text = _AUTHORIZATION.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HEX_RUN.sub(REDACTED, text)
    text = _B64_RUN.sub(REDACTED, text)
    if len(text) <= limit:
        return text
    cut = text[:max(0, limit)]
    if text[len(cut)] != " ":
        space = cut.rfind(" ")
        if space > len(cut) // 2:
            cut = cut[:space]
    cut = _MARKER_PARTIAL.sub("", cut).rstrip()
    return cut + "…"


def _json_rows(path: Path):
    try:
        with path.open(errors="replace") as handle:
            for raw in handle:
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] if isinstance(b, dict) and b.get("type") in {"text", "input_text"}
            and isinstance(b.get("text"), str) else b if isinstance(b, str) else ""
            for b in content)
    return ""


def _strip_claude_wrappers(text: str) -> str:
    text = _WRAPPERS.sub(" ", text)
    return _COMMAND_NAME.sub(lambda m: " " + m.group(1) + " ", text)


def _claude(path: Path, turn_id: str, limit: int) -> str | None:
    fallback = None
    for row in _json_rows(path):
        if not _is_genuine_user_row(row):
            continue
        message = _mapping(row.get("message"))
        if _first_text(row, "uuid", "id") == turn_id:
            found = row
            break
        if fallback is None and _explicit_turn_id(row, message) == turn_id:
            fallback = row
    else:
        found = fallback
    if found is None:
        return None
    message = _mapping(found.get("message"))
    return sanitize(_strip_claude_wrappers(_blocks_text(message.get("content", found.get("content")))), limit)


def _codex_text(row: dict, payload: dict) -> str:
    for mapping in (payload, row):
        for key in ("message", "text", "content"):
            found = _blocks_text(mapping.get(key))
            if found.strip():
                return found
    return ""


# Harness-injected context only; any other element (e.g. Codex goal mode's <objective>) is the user's prompt.
_CONTEXT_ELEMENT = re.compile(
    r"<(environment_context|user_instructions|permissions instructions|turn_aborted|user_shell_command|codex_internal_context)\b[^>]*>.*?</\1[^>]*>", re.S)
_OBJECTIVE = re.compile(r"<objective\b[^>]*>(.*?)</objective>", re.S)


def _codex_prompt(text: str) -> str:
    """The typed prompt: a goal-mode <objective> (even inside injected context), else the text minus injected context."""
    found = _OBJECTIVE.search(text)
    if found:
        return found.group(1).strip()
    return "" if _is_context_only(text) else _CONTEXT_ELEMENT.sub(" ", text).strip()


def _is_context_only(text: str) -> bool:
    """True for injected context (<environment_context>, <user_instructions>, AGENTS.md dumps), not a typed prompt."""
    text = text.strip()
    return text.startswith("# AGENTS.md instructions for") or not _CONTEXT_ELEMENT.sub("", text).strip()


def _codex_genuine(row_type: object, event_type: object, payload: dict) -> bool:
    return (row_type == "event_msg" and event_type in {"user_message", "user_input"}) or (
        row_type == "response_item" and event_type == "message" and payload.get("role") == "user")


def _codex(path: Path, turn_id: str, limit: int) -> str | None:
    """Replay collect_codex's turn assignment, tracking the user text behind each id."""
    last: str | None = None  # last genuine user text before the first row carrying the turn id
    after = False            # the turn id was seen without a preceding prompt: take the next genuine text
    seen_explicit = False
    for row in _json_rows(path):
        payload = _mapping(row.get("payload"))
        row_type, event_type = row.get("type"), payload.get("type")
        if row_type == "session_meta":
            continue
        explicit = _meta_text(_codex_event_turn_id(row, payload))
        genuine = _codex_genuine(row_type, event_type, payload)
        text = _codex_prompt(_codex_text(row, payload)) if genuine else ""
        ended = row_type == "event_msg" and event_type in _CODEX_TURN_END
        if ended and not explicit:
            seen_explicit = False
        if explicit:
            seen_explicit = not ended  # explicitness is per turn, as in collect_codex
            if explicit == turn_id:
                if text:
                    return sanitize(text, limit)
                if last:
                    return sanitize(last, limit)
                after = True
            else:
                last = None
                if after:
                    return None
            continue
        if row_type == "token_usage_record" or (row_type == "event_msg" and event_type == "token_count"):
            if after:
                return None
            continue
        if not text:
            continue
        if after:
            return sanitize(text, limit)
        last = text
        if not seen_explicit and _meta_text(_codex_user_event_identity(row, payload)) == turn_id:
            return sanitize(text, limit)
    return None


def _opencode(path: Path, session: str, turn_id: str, limit: int) -> str | None:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, OSError):
        return None
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT p.data FROM part AS p JOIN message AS m ON m.id = p.message_id "
            "WHERE m.id = ? AND m.session_id = ? ORDER BY p.time_created, p.id",
            (turn_id, session)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    texts = []
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "text" and isinstance(data.get("text"), str) \
                and not data.get("synthetic"):
            texts.append(data["text"])
    return sanitize("\n".join(texts), limit)


def _pi(path: Path, turn_id: str, limit: int) -> str | None:
    for row in _json_rows(path):
        message = _mapping(row.get("message"))
        if row.get("id") == turn_id and message.get("role") == "user":
            return sanitize(_blocks_text(message.get("content")), limit)
    return None


def extract_prompt(harness: str, source: object, session: object, turn_id: object, limit: int = 200) -> str | None:
    """Sanitized preview of the user prompt that started `turn_id`, or None. Never raises."""
    try:
        if not isinstance(turn_id, str) or not turn_id or not isinstance(source, (str, Path)):
            return None
        path = local_file(source)
        if path is None:
            return None
        if harness == "claude":
            return _claude(path, turn_id, limit)
        if harness == "codex":
            return _codex(path, turn_id, limit)
        if harness == "opencode":
            return _opencode(path, session, turn_id, limit) if isinstance(session, str) else None
        if harness == "pi":
            return _pi(path, turn_id, limit)
    except (OSError, ValueError, sqlite3.Error):
        return None
    return None
