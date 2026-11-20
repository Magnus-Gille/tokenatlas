#!/usr/bin/env python3
"""Stop-hook usage digest for Claude Code cloud sessions (TOKEN-201 proof of concept).

Runs inside an ephemeral cloud container. On every ``Stop`` it reads only the transcript lines added since the previous
run (a byte offset per file), reduces them to a digest in the *same JSONL shape* TokenAtlas's Claude collector already
reads, and writes it under a spool that mirrors the transcript layout, so the spool can be used as a Claude root:
``<state>/projects/<project>/<session>.jsonl`` and ``<session>/subagents/agent-*.jsonl``. If TOKENATLAS_CLOUD_URL (https)
is set it also POSTs the new lines (bearer token from TOKENATLAS_CLOUD_TOKEN); a failed POST leaves them queued.

The digest keeps request identity, timestamp, session, cwd, entrypoint, version, effort, model and an allowlist of numeric
usage counters. It drops all prompt, tool and assistant text; a prompt typed by the person becomes an empty marker row so
turn grouping still works.

The hook never blocks and never fails the session: every error is swallowed and the exit status is always 0.
Standard library only, so it runs in a bare container.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no lock, the hook is not meant to run there
    fcntl = None

STATE = Path(os.environ.get("TOKENATLAS_CLOUD_STATE", Path.home() / ".cache" / "tokenatlas-cloud"))
ROW_KEYS = ("uuid", "timestamp", "sessionId", "requestId", "cwd", "entrypoint", "version", "effort", "gitBranch",
            "parentSessionId", "agentId", "attributionAgent", "isSidechain", "turn_id", "turnId")
MESSAGE_KEYS = ("id", "model", "role", "stop_reason", "turn_id", "turnId")
COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
LABELS = ("service_tier", "speed", "inference_geo", "type")
_REMINDERS_ONLY = re.compile(r"\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.S)
_TOOL_RESULTS = ("tool_result", "tool_use_result")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _counters(source: object, keys=COUNTERS) -> dict:
    return {k: v for k in keys if isinstance(source, dict) and (v := _count(source.get(k))) is not None}


def safe_usage(usage: dict) -> dict:
    """Only numeric counters, the cache TTL split and a few short labels: nothing a prompt could travel in."""
    out = _counters(usage)
    split = _counters(usage.get("cache_creation"), ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    if split:
        out["cache_creation"] = split
    thinking = _counters(usage.get("output_tokens_details"), ("thinking_tokens",))
    if thinking:
        out["output_tokens_details"] = thinking
    for key in LABELS:
        value = usage.get(key)
        if isinstance(value, str) and len(value) <= 32 and re.fullmatch(r"[\w.\-]+", value):
            out[key] = value
    iterations = [dict(_counters(i), **({"cache_creation": s} if (s := _counters(i.get("cache_creation"), ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))) else {}))
                  for i in usage.get("iterations", []) if isinstance(i, dict)] if isinstance(usage.get("iterations"), list) else []
    if iterations:
        out["iterations"] = iterations
    return out


def _injected_only(content) -> bool:
    """Mirror of the collector's rule: a user row that is only an interruption marker or system reminders is not a prompt."""
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        if any(isinstance(i, dict) and i.get("type") in (*_TOOL_RESULTS, "image") for i in content):
            return False
        texts = [i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"]
        if not texts:
            return False
    else:
        return False
    joined = "\n".join(t for t in texts if isinstance(t, str)).strip()
    return bool(joined) and (joined.startswith("[Request interrupted by user") or bool(_REMINDERS_ONLY.fullmatch(joined)))


def _genuine_user(row: dict) -> bool:
    """A prompt typed by the person, by the same rule as the collector's (why._is_genuine_user_row)."""
    if row.get("type") != "user" or row.get("isMeta") is True or row.get("isCompactSummary") is True:
        return False
    content = (row.get("message") if isinstance(row.get("message"), dict) else {}).get("content", row.get("content"))
    if _injected_only(content):
        return False
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return True
    return any(not isinstance(i, dict) or i.get("type") not in _TOOL_RESULTS for i in content)


def digest_row(row: object) -> dict | None:
    """The digest of one transcript row, or None when it carries neither usage nor a person's prompt."""
    if not isinstance(row, dict):
        return None
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    out = {k: row[k] for k in ROW_KEYS if isinstance(row.get(k), (str, bool)) and row.get(k) is not None}
    if row.get("type") == "assistant" and isinstance(message.get("usage"), dict):
        out["type"] = "assistant"
        out["message"] = {k: message[k] for k in MESSAGE_KEYS if isinstance(message.get(k), str)}
        out["message"]["usage"] = safe_usage(message["usage"])
        return out
    if _genuine_user(row):
        out["type"] = "user"  # no message.content: the collector counts a user row without a content list as a prompt
        out["message"] = {"role": "user"}
        return out
    return None


def new_digest(transcript: Path, offset: int) -> tuple[list[dict], int]:
    """Digest rows from complete lines after ``offset`` and the new offset (a half-written last line is left for later)."""
    with transcript.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    complete = data[: data.rfind(b"\n") + 1]
    rows = []
    for line in complete.splitlines():
        try:
            digest = digest_row(json.loads(line))
        except ValueError:
            continue
        if digest:
            rows.append(digest)
    return rows, offset + len(complete)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirected POST would silently become a GET with no body


def _post(url: str, token: str, body: bytes) -> bool:
    local = re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", url)
    if not url.startswith("https://") and not local:
        return False  # never send a bearer token in clear text
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-ndjson", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):  # a refusal, a redirect left unfollowed, or a dropped connection
        return False


def _files(transcript: Path) -> list[tuple[str, Path]]:
    """(path relative to the session, file): the main transcript and any subagent transcripts beside it."""
    found = [("", transcript)]
    for sub in sorted((transcript.with_suffix("") / "subagents").glob("agent-*.jsonl")):
        found.append((f"subagents/{sub.name}", sub))
    return found


def _head(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.sha256(fh.read(512)).hexdigest()[:16]


def _read_offset(file: Path, transcript: Path) -> int:
    try:
        saved = json.loads(file.read_text())
        offset, head = int(saved["offset"]), saved["head"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if offset > transcript.stat().st_size or head != _head(transcript):
        return 0  # rewritten (compaction): start over, receivers dedup on request id
    return offset


def run(payload: dict, state: Path = STATE) -> int:
    """Handle one Stop payload; returns the number of digest rows queued."""
    transcript = Path(payload.get("transcript_path") or "")
    session = str(payload.get("session_id") or transcript.stem)
    if not transcript.is_file() or not session.replace("-", "").isalnum():
        return 0
    state.mkdir(parents=True, exist_ok=True)
    with (state / f"{session}.lock").open("w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)  # overlapping Stops take turns
        return _run_locked(transcript, session, state)


def _run_locked(transcript: Path, session: str, state: Path) -> int:
    project = transcript.parent.name or "project"
    total, pending = 0, state / f"{session}.pending.jsonl"
    for relative, file in _files(transcript):
        offset_file = state / f"{session}.{hashlib.sha256(relative.encode()).hexdigest()[:8]}.offset"
        rows, offset = new_digest(file, _read_offset(offset_file, file))
        if rows:
            spool = state / "projects" / project / (f"{session}.jsonl" if not relative else f"{session}/{relative}")
            spool.parent.mkdir(parents=True, exist_ok=True)
            with spool.open("a") as fh:
                fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
            with pending.open("a") as fh:
                fh.write("".join(json.dumps({**r, "_path": relative}, separators=(",", ":")) + "\n" for r in rows))
        offset_file.write_text(json.dumps({"offset": offset, "head": _head(file)}))
        total += len(rows)
    url, token = os.environ.get("TOKENATLAS_CLOUD_URL"), os.environ.get("TOKENATLAS_CLOUD_TOKEN", "")
    if url and pending.exists() and pending.stat().st_size:
        try:
            body = pending.read_bytes()
            if _post(url, token, body):
                remainder = pending.read_bytes()[len(body):]
                pending.write_bytes(remainder)
        except Exception:  # noqa: BLE001 - offline or refused: keep the lines queued
            pass
    return total


def main() -> int:
    try:
        run(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
