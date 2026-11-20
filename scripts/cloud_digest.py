#!/usr/bin/env python3
"""Stop-hook usage digest for Claude Code cloud sessions (TOKEN-201 proof of concept).

Runs inside an ephemeral cloud container. On every ``Stop`` it reads only the transcript lines added since the previous
run (a byte offset kept per session), reduces them to a digest in the *same JSONL shape* TokenAtlas's Claude collector
already reads, and appends it to a local spool. If TOKENATLAS_CLOUD_URL is set it also POSTs the new digest lines
(bearer token from TOKENATLAS_CLOUD_TOKEN); a failed POST leaves them queued for the next Stop.

The digest keeps request identity, timestamp, session, cwd, entrypoint, version, effort, model and usage. It drops all
prompt, tool and assistant text. User prompts are reduced to an empty marker row so turn grouping still works.

The hook never blocks and never fails the session: every error is swallowed and the exit status is always 0.
Standard library only, so it runs in a bare container.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("TOKENATLAS_CLOUD_STATE", Path.home() / ".cache" / "tokenatlas-cloud"))
ROW_KEYS = ("uuid", "timestamp", "sessionId", "requestId", "cwd", "entrypoint", "version", "effort", "gitBranch",
            "parentSessionId", "agentId", "isSidechain")
MESSAGE_KEYS = ("id", "model", "role", "usage", "stop_reason")


def _genuine_user(row: dict) -> bool:
    """A prompt typed by the person: not a tool result and not text Claude Code injected."""
    if row.get("type") != "user" or row.get("isMeta") is True or row.get("isCompactSummary") is True:
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith(("<system-reminder>", "[Request interrupted"))
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                   and not b["text"].lstrip().startswith(("<system-reminder>", "[Request interrupted")) for b in content) \
            and not any(isinstance(b, dict) and b.get("type") in ("tool_result", "tool_use_result") for b in content)
    return False


def digest_row(row: dict) -> dict | None:
    """The digest of one transcript row, or None when it carries neither usage nor a person's prompt."""
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    out = {k: row[k] for k in ROW_KEYS if row.get(k) is not None}
    if row.get("type") == "assistant" and isinstance(message.get("usage"), dict):
        usage = {k: v for k, v in message["usage"].items() if k not in ("iterations", "server_tool_use")}
        out["type"] = "assistant"
        out["message"] = {k: message[k] for k in MESSAGE_KEYS if k in message}
        out["message"]["usage"] = usage
        return out
    if _genuine_user(row):
        out["type"] = "user"  # no message.content: TokenAtlas counts a user row without a content list as a prompt
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


def _post(url: str, token: str, body: bytes) -> bool:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-ndjson", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - the URL is the operator's own endpoint
        return 200 <= resp.status < 300


def run(payload: dict, state: Path = STATE) -> int:
    """Handle one Stop payload; returns the number of digest rows queued."""
    transcript = Path(payload.get("transcript_path") or "")
    session = str(payload.get("session_id") or transcript.stem)
    if not transcript.is_file() or not session.replace("-", "").isalnum():
        return 0
    state.mkdir(parents=True, exist_ok=True)
    offset_file, spool, pending = state / f"{session}.offset", state / f"{session}.digest.jsonl", state / f"{session}.pending.jsonl"
    try:
        offset = int(offset_file.read_text())
    except (OSError, ValueError):
        offset = 0
    if offset > transcript.stat().st_size:  # the transcript was rewritten (compaction): start over, receivers dedup
        offset = 0
    rows, offset = new_digest(transcript, offset)
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    if text:
        with spool.open("a") as fh:
            fh.write(text)
        with pending.open("a") as fh:
            fh.write(text)
    offset_file.write_text(str(offset))
    url, token = os.environ.get("TOKENATLAS_CLOUD_URL"), os.environ.get("TOKENATLAS_CLOUD_TOKEN", "")
    if url and pending.exists() and pending.stat().st_size:
        try:
            if _post(url, token, pending.read_bytes()):
                pending.write_text("")
        except Exception:  # noqa: BLE001 - offline or refused: keep the lines queued
            pass
    return len(rows)


def main() -> int:
    try:
        run(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
