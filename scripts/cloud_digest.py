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
import http.client
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


def _iteration(item: dict) -> dict:
    out = _counters(item)
    split = _counters(item.get("cache_creation"), ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    if split:
        out["cache_creation"] = split
    for key in ("type", "model"):  # an advisor iteration is priced on its own model: the receiver needs both
        value = item.get(key)
        if isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"[\w.\-\[\]:]+", value):
            out[key] = value
    return out


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
    iterations = [_iteration(i) for i in usage.get("iterations", []) if isinstance(i, dict)] if isinstance(usage.get("iterations"), list) else []
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
    for key in ("cwd", "gitBranch"):  # optional metadata: dropped when the receiver would reject the whole row for it
        if isinstance(out.get(key), str) and (len(out[key]) > 200 or not out[key].isprintable()):
            del out[key]
    if row.get("type") == "assistant" and isinstance(message.get("usage"), dict):
        out["type"] = "assistant"
        out["message"] = {k: message[k] for k in MESSAGE_KEYS if isinstance(message.get(k), str)}
        if not re.match(r"[A-Za-z0-9]", out["message"].get("model", "x")):
            del out["message"]["model"]  # "<synthetic>" error rows: the receiver refuses a model that starts oddly
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


import time

CLIENT_PROTOCOL = "2"  # X-TokenAtlas-Client: the receiver answers 426 to a client that still clears its queue on any 2xx
USER_AGENT = f"tokenatlas-cloud-digest/{CLIENT_PROTOCOL}"
MAX_ROWS = 200  # API limits (docs/architecture/cloud-ingest.md section 3)
MAX_BODY = 250 * 1024  # a little under the 256 KB request cap
MAX_ROW = 16 * 1024
BUDGET = 4.5  # seconds of the hook's 5 s limit that this process may use
POST_TIMEOUT = 3.0
QUARANTINE_CAP = 1024 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
REL_PATH = re.compile(r"(subagents/(workflows/wf_[A-Za-z0-9_-]{1,64}/)?agent-[A-Za-z0-9_-]{1,64}\.jsonl)?")
PERMANENT = ("conflict", "rejected", "row_too_large", "suppressed")  # never retried: moved to quarantine


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirected POST would silently become a GET with no body


class PostResult:
    """Outcome of one POST. Truthy only for a 2xx; ``status`` is None when no HTTP answer arrived at all."""

    def __init__(self, status=None, retry_after=None, body=b""):
        self.status, self.retry_after, self.body = status, retry_after, body

    def __bool__(self):
        return self.status is not None and 200 <= self.status < 300


def _post(url: str, token: str, body: bytes, timeout: float = POST_TIMEOUT) -> PostResult:
    local = re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", url)
    if not url.startswith("https://") and not local:
        return PostResult()  # never send a bearer token in clear text
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-ndjson", "Authorization": f"Bearer {token}",
        "X-TokenAtlas-Client": CLIENT_PROTOCOL, "User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=max(0.2, timeout)) as resp:
            return PostResult(resp.status, None, resp.read(1024 * 1024))
    except urllib.error.HTTPError as exc:  # a refusal that still carries a status (and, for 429, Retry-After)
        retry = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return PostResult(exc.code, int(retry) if retry and retry.isdigit() else None, exc.read(1024 * 1024))
        except OSError:
            return PostResult(exc.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):  # a redirect left unfollowed, a dropped connection, a timeout
        return PostResult()


def _files(transcript: Path) -> list[tuple[str, Path]]:
    """(path relative to the session, file): the main transcript, its subagent transcripts and workflow subagents."""
    found = [("", transcript)]
    base = transcript.with_suffix("") / "subagents"
    for pattern in ("agent-*.jsonl", "workflows/wf_*/agent-*.jsonl"):
        for sub in sorted(base.glob(pattern)):
            relative = sub.relative_to(base.parent).as_posix()
            if REL_PATH.fullmatch(relative):  # names the receiver would reject are not queued
                found.append((relative, sub))
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
        return 0  # rewritten (compaction): start over, receivers dedup on row identity
    return offset


# --- send state: back-off, disabled flag and the queue counters, one small JSON file per state directory -------------------

def _load_send_state(state: Path) -> dict:
    try:
        data = json.loads((state / "send-state.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_send_state(state: Path, data: dict, before: dict, session: str) -> None:
    """Merge this hook's changes into the file as it is now: sessions share it, each only owns what it changed."""
    try:
        with (state / "send-state.lock").open("w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            now = _load_send_state(state)
            for key in ("backoff_until", "failures", "disabled_for", "last_ok"):
                if data.get(key) != before.get(key):
                    if data.get(key) is None:
                        now.pop(key, None)
                    else:
                        now[key] = data[key]
            now["quarantined"] = int(now.get("quarantined", 0)) + int(data.get("quarantined", 0)) - int(before.get("quarantined", 0))
            if session in data.get("queued", {}):
                now.setdefault("queued", {})[session] = data["queued"][session]
            tmp = state / f"send-state.json.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(now, separators=(",", ":")))
            os.replace(tmp, state / "send-state.json")
    except OSError:
        pass


def _token_tag(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _warn(state: Path, text: str) -> None:
    try:
        (state / "WARNING.txt").write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n")
    except OSError:
        pass


def _quarantine(state: Path, send: dict, line: bytes, status: str, code: str | None = None) -> None:
    send["quarantined"] = int(send.get("quarantined", 0)) + 1
    path = state / "quarantine.jsonl"
    try:
        if path.exists() and path.stat().st_size > QUARANTINE_CAP:
            return  # counted, not stored: a bad batch must not fill the disk
        with path.open("a") as fh:
            fh.write(json.dumps({"status": status, "code": code, "row": line.decode("utf-8", "replace").strip()}, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _backoff(send: dict, retry_after: int | None) -> None:
    send["failures"] = int(send.get("failures", 0)) + 1
    delay = retry_after if retry_after is not None else min(30 * 2 ** (send["failures"] - 1), 3600)
    send["backoff_until"] = time.time() + min(max(delay, 1), 3600)


def _from_worker(res: PostResult) -> bool:
    try:
        return isinstance(json.loads(res.body).get("error"), dict)
    except (ValueError, AttributeError, TypeError):
        return False


def _take(lines: list[bytes], max_rows: int) -> int:
    """How many leading lines fit one request (row and byte caps); always at least one."""
    count, size = 0, 0
    for line in lines[:max_rows]:
        if count and size + len(line) > MAX_BODY:
            break
        count, size = count + 1, size + len(line)
    return max(count, 1)


def _write_queue(pending: Path, lines: list[bytes]) -> None:
    if not lines:
        pending.write_bytes(b"")
        return
    tmp = pending.with_name(pending.name + ".tmp")
    tmp.write_bytes(b"".join(lines))
    os.replace(tmp, pending)


def _send_queue(pending: Path, session: str, url: str, token: str, state: Path, deadline: float, last_chance: bool = False) -> None:
    """Send what is queued, within the time budget. Per-line receipts decide what leaves the queue (design section 7)."""
    send = _load_send_state(state)
    before = json.loads(json.dumps(send))
    try:
        if send.get("disabled_for") in (_token_tag(token), f"426:{CLIENT_PROTOCOL}"):
            return  # the token (or this client) was refused; nothing changes until the token or the script does
        if time.time() < float(send.get("backoff_until", 0)) and not last_chance:
            return  # SessionEnd is the container's last chance, so it tries anyway
        lines = [l for l in pending.read_bytes().splitlines(keepends=True) if l.endswith(b"\n")] if pending.exists() else []
        max_rows = MAX_ROWS
        while lines and deadline - time.monotonic() > 0.5:
            if len(lines[0]) > MAX_ROW:  # one oversized row must not block the queue
                _quarantine(state, send, lines.pop(0), "row_too_large", "client_size_check")
                _write_queue(pending, lines)
                continue
            n = _take(lines, max_rows)
            batch = lines[:n]
            res = _post(url, token, b"".join(batch), min(POST_TIMEOUT, deadline - time.monotonic() - 0.2))
            if res.status == 200:
                try:
                    results = json.loads(res.body)["results"]
                    statuses = [(r["status"], r.get("code")) for r in results]
                    if len(statuses) != len(batch):
                        raise ValueError("incomplete receipt")
                except (ValueError, KeyError, TypeError):
                    _backoff(send, None)  # a missing or short receipt keeps the whole batch
                    break
                kept = []
                for line, (status, code) in zip(batch, statuses):
                    if status in ("accepted", "duplicate"):
                        continue
                    if status in PERMANENT:
                        _quarantine(state, send, line, status, code)
                    else:
                        kept.append(line)  # quota or an unknown status: retry later
                lines = kept + lines[n:]
                _write_queue(pending, lines)
                if kept:
                    _backoff(send, 300)  # quota: do not hammer until the next window
                    break
                send["failures"], send["last_ok"] = 0, time.time()
            elif res.status == 413:
                if n > 1:
                    max_rows = max(1, n // 2)
                    continue
                _quarantine(state, send, lines.pop(0), "row_too_large", "http_413")
                _write_queue(pending, lines)
            elif res.status in (401, 403, 426) and not _from_worker(res):
                _backoff(send, None)  # an edge or proxy refusal says nothing about the token
                break
            elif res.status in (401, 403):
                send["disabled_for"] = _token_tag(token)
                _warn(state, f"ingest token rejected (HTTP {res.status}); sending is off until TOKENATLAS_CLOUD_TOKEN changes")
                break
            elif res.status == 426:
                send["disabled_for"] = f"426:{CLIENT_PROTOCOL}"
                _warn(state, "ingest endpoint requires a newer cloud_digest.py (HTTP 426); sending is off until it is updated")
                break
            else:  # 429, 5xx, timeout, refusal: keep everything and wait
                _backoff(send, res.retry_after if res.status == 429 else None)
                break
        send.setdefault("queued", {})[session] = len(lines)  # what a reclaimed container would lose
    finally:
        _save_send_state(state, send, before, session)


def _digest_session(transcript: Path, session: str, state: Path, wait: float) -> int:
    """Digest new transcript lines (main, subagents, workflow subagents) into the spool and the pending queue."""
    project = transcript.parent.name or "project"
    total, pending = 0, state / f"{session}.pending.jsonl"
    for attempt in range(2):
        for relative, file in _files(transcript):
            offset_file = state / f"{session}.{hashlib.sha256(relative.encode()).hexdigest()[:8]}.offset"
            rows, offset = new_digest(file, _read_offset(offset_file, file))
            if rows:
                spool = state / "projects" / project / (f"{session}.jsonl" if not relative else f"{session}/{relative}")
                spool.parent.mkdir(parents=True, exist_ok=True)
                with spool.open("a") as fh:
                    fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
                with pending.open("a") as fh:
                    fh.write("".join(json.dumps({**r, "session": session, "_path": relative}, separators=(",", ":")) + "\n" for r in rows))
            offset_file.write_text(json.dumps({"offset": offset, "head": _head(file)}))
            total += len(rows)
        # The transcript can lag the hook: when this Stop found nothing new, give the final turn a moment to land.
        if attempt or total or wait <= 0 or not _grew(transcript, wait):
            break
    return total


def _grew(transcript: Path, wait: float) -> bool:
    size = transcript.stat().st_size
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.1)
        if transcript.stat().st_size != size:
            return True
    return False


def _locked(state: Path, session: str, blocking: bool):
    lock = (state / f"{session}.lock").open("w")
    if fcntl:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            lock.close()
            return None
    return lock


def run(payload: dict, state: Path = STATE) -> int:
    """Handle one hook payload (Stop by default); returns the number of digest rows queued."""
    deadline = time.monotonic() + BUDGET
    event = payload.get("hook_event_name")
    transcript = Path(payload.get("transcript_path") or "")
    session = str(payload.get("session_id") or transcript.stem)
    url, token = os.environ.get("TOKENATLAS_CLOUD_URL"), os.environ.get("TOKENATLAS_CLOUD_TOKEN", "")
    total, handled = 0, False
    state.mkdir(parents=True, exist_ok=True)
    if transcript.is_file() and SESSION_ID.fullmatch(session):
        lock = _locked(state, session, blocking=True)  # overlapping hooks take turns
        if lock:
            handled = True
            with lock:
                wait = float(os.environ.get("TOKENATLAS_CATCHUP_SECONDS", "1.0")) if event in ("Stop", "SessionEnd") else 0.0
                total = _digest_session(transcript, session, state, min(wait, max(0.0, deadline - time.monotonic() - 2.0)))
                pending = state / f"{session}.pending.jsonl"
                if url and pending.exists() and pending.stat().st_size:
                    try:
                        _send_queue(pending, session, url, token, state, deadline, event == "SessionEnd")
                    except Exception:  # noqa: BLE001 - offline or refused: keep the lines queued
                        pass
    if url and event in ("SessionStart", "SessionEnd"):
        # A resumed or ending container flushes the queues other sessions left behind.
        for other in sorted(state.glob("*.pending.jsonl")):
            name = other.name[: -len(".pending.jsonl")]
            if (name == session and handled) or not SESSION_ID.fullmatch(name) or time.monotonic() > deadline - 0.5:
                continue
            lock = _locked(state, name, blocking=False)
            if lock:
                with lock:
                    try:
                        _send_queue(other, name, url, token, state, deadline, event == "SessionEnd")
                    except Exception:  # noqa: BLE001
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
