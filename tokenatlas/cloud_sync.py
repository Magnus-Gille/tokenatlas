"""Pull cloud-session usage from the ingest service into a local `cloud` Claude root (TOKEN-210, design section 8).

The service stores digest rows (counters and ids, no text) that cloud sessions could not keep. `sync-cloud` pulls them after a
cursor, stores each event once by `dedupe_key`, rebuilds per-session transcripts in the layout the Claude collector reads and
lets the normal refresh import them. Everything that comes back is untrusted: ids and paths are validated, rows are reduced
to the same allowlist the hook uses, and a cloud `cwd` is only a label (it never reaches filesystem discovery).

State lives in `cloud/` next to the history database, not in it: the cursor and the event table commit together in one
transaction, and a damaged or deleted state directory only means a full re-pull.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

PAGE_LIMIT = 1000
MAX_PAGES = 200  # a runaway server cannot keep one invocation busy forever
MAX_PAGE_BYTES = 8 * 1024 * 1024
TIMEOUT = 30.0
def _retention_days():
    try:
        return max(1, int(os.environ.get("TOKENATLAS_CLOUD_RETENTION_DAYS", "90")))
    except ValueError:
        return 90


SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
REL_PATH = re.compile(r"(subagents/(workflows/wf_[A-Za-z0-9_-]{1,64}/)?agent-[A-Za-z0-9_-]{1,64}\.jsonl)?")
DEDUPE_KEY = re.compile(r"[0-9a-f]{64}")
ROW_KEYS = ("uuid", "timestamp", "sessionId", "requestId", "cwd", "entrypoint", "version", "effort", "gitBranch",
            "parentSessionId", "agentId", "attributionAgent", "isSidechain", "turn_id", "turnId")
MESSAGE_KEYS = ("id", "model", "role", "stop_reason", "turn_id", "turnId")
COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
LABELS = ("service_tier", "speed", "inference_geo", "type")


class CloudSyncError(ValueError):
    """A configuration or service problem the person can act on."""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 53 else None


def _counters(source, keys=COUNTERS):
    return {k: v for k in keys if isinstance(source, dict) and (v := _count(source.get(k))) is not None}


def _label(value, limit, pattern):
    return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(pattern, value) else None


def _iteration(item):
    out = _counters(item)
    split = _counters(item.get("cache_creation"), ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    if split:
        out["cache_creation"] = split
    for key in ("type", "model"):
        if (v := _label(item.get(key), 64, r"[\w.\-\[\]:]+")) is not None:
            out[key] = v
    return out


def _usage(usage):
    out = _counters(usage)
    split = _counters(usage.get("cache_creation"), ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    if split:
        out["cache_creation"] = split
    thinking = _counters(usage.get("output_tokens_details"), ("thinking_tokens",))
    if thinking:
        out["output_tokens_details"] = thinking
    for key in LABELS:
        if (v := _label(usage.get(key), 32, r"[\w.\-]+")) is not None:
            out[key] = v
    if isinstance(usage.get("iterations"), list):
        iterations = [_iteration(i) for i in usage["iterations"][:64] if isinstance(i, dict)]
        if iterations:
            out["iterations"] = iterations
    return out


def clean_row(row):
    """The row reduced to what a digest may carry, or None when it is not a digest row at all."""
    if not isinstance(row, dict) or row.get("type") not in ("assistant", "user"):
        return None
    out = {k: row[k] for k in ROW_KEYS if isinstance(row.get(k), (str, bool)) and (not isinstance(row[k], str) or len(row[k]) <= 512)}
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    out["type"] = row["type"]
    out["message"] = {k: message[k] for k in MESSAGE_KEYS if isinstance(message.get(k), str) and len(message[k]) <= 512}
    if row["type"] == "assistant":
        if not isinstance(message.get("usage"), dict):
            return None
        out["message"]["usage"] = _usage(message["usage"])
    else:
        out["message"] = {"role": "user"}  # a prompt marker only; the collector counts it as a turn start
    return out


def clean_envelope(line):
    """(envelope fields, cleaned row) for one pulled NDJSON line, or None when anything about it is off."""
    try:
        env = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(env, dict):
        return None
    seq, key, session, path = env.get("seq"), env.get("dedupe_key"), env.get("session_id"), env.get("rel_path", "")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 and isinstance(key, str) and DEDUPE_KEY.fullmatch(key)):
        return None
    if not (isinstance(session, str) and SESSION_ID.fullmatch(session) and isinstance(path, str) and REL_PATH.fullmatch(path)):
        return None
    row = clean_row(env.get("row"))
    if row is None:
        return None
    text = lambda v, n: v if isinstance(v, str) and len(v) <= n and re.fullmatch(r"[\w.:\-]*", v) else ""
    received = env.get("received_at")
    received = received if isinstance(received, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", received) else ""
    if isinstance(row.get("cwd"), str):  # a cloud path is a label only: a non-absolute id never reaches local project discovery
        row["cwd"] = "cloud:" + (re.sub(r"[^\w.\-]", "_", row["cwd"].rstrip("/").rsplit("/", 1)[-1])[:64] or "unknown")
    return dict(seq=seq, dedupe_key=key, session_id=session, rel_path=path, source=text(env.get("source"), 64),
                token_id=text(env.get("token_id"), 64), received_at=received), row


# --- configuration -----------------------------------------------------------------------------------------------------

def endpoint():
    """(events URL, read token) from the environment; CloudSyncError when sync is not set up."""
    base = os.environ.get("TOKENATLAS_CLOUD_PULL_URL") or ""
    if not base:
        ingest = os.environ.get("TOKENATLAS_CLOUD_URL") or ""
        base = re.sub(r"/v1/ingest/?$", "/v1/events", ingest) if ingest else ""
    token = os.environ.get("TOKENATLAS_CLOUD_READ_TOKEN", "").strip()
    if not base or not token:
        raise CloudSyncError("cloud sync is not configured: set TOKENATLAS_CLOUD_URL (or TOKENATLAS_CLOUD_PULL_URL, the /v1/events URL) "
                             "and TOKENATLAS_CLOUD_READ_TOKEN (a `read` token)")
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        raise CloudSyncError("TOKENATLAS_CLOUD_READ_TOKEN must be printable ASCII without spaces")
    if not (base.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", base)):
        raise CloudSyncError("the cloud URL must be https:// (plain http only for loopback); a bearer token is never sent in clear text")
    return base, token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get(url, token):
    """(status, headers, body); the service's answer, or CloudSyncError when none arrived."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "tokenatlas-sync-cloud/1",
                                               "Accept": "application/x-ndjson"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}) if re.match(r"http://", url) else urllib.request.ProxyHandler(), _NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers, exc.read(64 * 1024)
        except OSError:
            return exc.code, exc.headers, b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CloudSyncError(f"the cloud service did not answer ({type(exc).__name__})") from None  # the text can echo request headers


# --- local store -------------------------------------------------------------------------------------------------------

def directory(db):
    return Path(db).expanduser().parent / "cloud"


def _open(folder):
    folder.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(folder / "events.sqlite3", timeout=30)
    try:
        _schema(c)
    except BaseException:
        c.close()
        raise
    return c


def _schema(c):
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            dedupe_key TEXT PRIMARY KEY, generation INTEGER, seq INTEGER NOT NULL, source TEXT, token_id TEXT,
            session_id TEXT NOT NULL, rel_path TEXT NOT NULL, received_at TEXT, row TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dirty (session_id TEXT PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS events_session ON events (session_id, rel_path, generation, seq);
    """)


def _state(c, key, default=None):
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(c, key, value):
    c.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _generation(cursor):
    """The generation inside an opaque cursor, for ordering only; 0 when it cannot be read."""
    try:
        pad = "=" * (-len(cursor) % 4)
        g = json.loads(base64.urlsafe_b64decode(cursor + pad)).get("g")
        return g if isinstance(g, int) and not isinstance(g, bool) else 0
    except (ValueError, TypeError):
        return 0


def _write_root(c, folder, sessions):
    """Rebuild the transcripts of the given sessions from the stored events, in delivery order."""
    root = folder / "projects" / "cloud"
    for session in sorted(sessions):
        paths = [r[0] for r in c.execute("SELECT DISTINCT rel_path FROM events WHERE session_id=?", (session,))]
        for rel in paths:
            target = root / (f"{session}.jsonl" if not rel else f"{session}/{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for (row,) in c.execute("SELECT row FROM events WHERE session_id=? AND rel_path=? ORDER BY generation, seq", (session, rel)):
                    fh.write(row + "\n")
            os.replace(tmp, target)
    with c:
        c.executemany("DELETE FROM dirty WHERE session_id=?", [(s,) for s in sessions])
    return root


def _flush(c, folder):
    """Rebuild every session whose stored events are not in the root yet (an interrupted pull, a deleted root)."""
    if not (folder / "projects" / "cloud").is_dir():
        with c:
            c.execute("INSERT OR IGNORE INTO dirty SELECT DISTINCT session_id FROM events")
    pending = [r[0] for r in c.execute("SELECT session_id FROM dirty")]
    _write_root(c, folder, pending)
    (folder / "projects" / "cloud").mkdir(parents=True, exist_ok=True)
    return len(pending)


# --- pull ----------------------------------------------------------------------------------------------------------------

def pull(db, now=None):
    """Pull everything after the stored cursor; returns a result dict. The root is rebuilt only for sessions that gained events."""
    url, token = endpoint()
    folder = directory(db)
    try:
        c = _open(folder)
    except (OSError, sqlite3.Error) as exc:
        raise CloudSyncError(f"the local cloud store cannot be opened ({type(exc).__name__}); delete {folder} to start over") from None
    now = now if now is not None else time.time()
    result = dict(pages=0, events_new=0, events_seen=0, rejected=0, sessions_touched=0, reset=False, root=str(folder / "projects"))
    touched = set()
    try:
        cursor = _state(c, "cursor")
        last_pull = float(_state(c, "last_pull_at", 0) or 0)
        if cursor and last_pull and now - last_pull > _retention_days() * 86400:
            result["warning"] = (f"the last pull was more than {_retention_days()} days ago: rows the service already deleted are missing "
                                 "(set TOKENATLAS_CLOUD_RETENTION_DAYS to the service's retention)")
        for _ in range(MAX_PAGES):
            query = f"?limit={PAGE_LIMIT}" + (f"&after={cursor}" if cursor else "")
            status, headers, body = _get(url + query, token)
            if status == 410 and cursor:
                cursor = None  # expired or reset: re-pull from zero, the event table absorbs the overlap by dedupe_key
                result["reset"] = True
                with c:
                    c.execute("DELETE FROM state WHERE key='cursor'")
                continue
            if status in (401, 403):
                raise CloudSyncError(f"the cloud service refused the read token (HTTP {status})")
            if status != 200:
                raise CloudSyncError(f"the cloud service answered HTTP {status}")
            if len(body) > MAX_PAGE_BYTES:
                raise CloudSyncError("the cloud service sent an oversized page")
            next_cursor = headers.get("X-Next-Cursor") or ""
            if next_cursor and not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", next_cursor):
                raise CloudSyncError("the cloud service sent a malformed cursor")
            lines = [l for l in body.split(b"\n") if l.strip()]
            if lines and not next_cursor:
                raise CloudSyncError("the cloud service sent a page without a cursor")
            generation = _generation(next_cursor)
            with c:  # the page and the new cursor commit together
                for line in lines:
                    parsed = clean_envelope(line)
                    result["events_seen"] += 1
                    if parsed is None:
                        result["rejected"] += 1
                        continue
                    env, row = parsed
                    cur = c.execute("INSERT OR IGNORE INTO events (dedupe_key, generation, seq, source, token_id, session_id, rel_path, received_at, row) "
                                    "VALUES (?,?,?,?,?,?,?,?,?)",
                                    (env["dedupe_key"], generation, env["seq"], env["source"], env["token_id"], env["session_id"],
                                     env["rel_path"], env["received_at"], json.dumps(row, separators=(",", ":"))))
                    if cur.rowcount:
                        result["events_new"] += 1
                        touched.add(env["session_id"])
                        c.execute("INSERT OR IGNORE INTO dirty VALUES (?)", (env["session_id"],))
                        if env["received_at"] > _state(c, "last_event_at", ""):
                            _set(c, "last_event_at", env["received_at"])
                if next_cursor:
                    _set(c, "cursor", next_cursor)
                _set(c, "last_pull_at", int(now))
            result["pages"] += 1
            if not lines:
                with c:
                    _set(c, "caught_up", 1)
                break
            cursor = next_cursor or cursor
        else:
            with c:
                _set(c, "caught_up", 0)
            result["warning"] = "stopped after the page limit; run sync-cloud again to continue"
        result["sessions_touched"] = len(touched)
        result.update(freshness(c, now))
        return result
    except (OSError, sqlite3.Error) as exc:
        raise CloudSyncError(f"the local cloud store failed ({type(exc).__name__}); delete {folder} to start over") from None
    finally:
        try:
            result["sessions_rebuilt"] = _flush(c, folder)  # also after an interrupted pull: committed pages always reach the root
        except (OSError, sqlite3.Error):
            pass
        c.close()


QUIET_SECONDS = 2 * 3600  # rows only ship at a Stop, so a long turn is silent: a session quiet for less than this may simply be running


def session_gaps(c, now=None):
    """Sessions that end on a prompt or a `tool_use` row with nothing after it (TOKEN-212), most recently active first.

    The hook ships an assistant row with stop_reason `tool_use` and a genuine user prompt, never the tool results between them, so a
    session that finished normally ends on an assistant row with another stop reason. One that ends otherwise was interrupted by the
    person, is still running, or lost its tail (the container died before the final flush): the rows cannot tell which. Sessions quiet
    for under QUIET_SECONDS are not counted, and nothing is counted while the pull has not caught up (the tail may still be on the
    server). A tail lost after an intermediate row is invisible here: this is a visible hint, not proof of completeness.
    """
    now = now if now is not None else time.time()
    if _state(c, "caught_up") != "1":
        return []
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - QUIET_SECONDS))
    out = []
    for session, received, last_type, stop in c.execute(
            "SELECT e.session_id, (SELECT MAX(received_at) FROM events x WHERE x.session_id = e.session_id), "
            "json_extract(e.row, '$.type'), json_extract(e.row, '$.message.stop_reason') FROM events e "
            "WHERE e.rel_path = '' AND e.rowid = (SELECT rowid FROM events y WHERE y.session_id = e.session_id AND y.rel_path = '' "
            "ORDER BY y.generation DESC, y.seq DESC LIMIT 1) ORDER BY e.session_id"):
        if (last_type == "user" or (last_type == "assistant" and stop == "tool_use")) and received and received < cutoff:
            out.append((received, session))
    return [s for _, s in sorted(out, reverse=True)]


def freshness(c, now=None):
    """Numbers for the report's cloud line: sessions, rows, the newest event and the last pull."""
    now = now if now is not None else time.time()
    sessions = c.execute("SELECT COUNT(DISTINCT session_id) FROM events").fetchone()[0]
    events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    gaps = session_gaps(c, now)
    return dict(sessions=sessions, events=events, last_event_at=_state(c, "last_event_at") or None,
                last_pull_at=int(float(_state(c, "last_pull_at", 0) or 0)) or None,
                incomplete=len(gaps), incomplete_sessions=gaps[:20])


def status(db):
    """Freshness of the local cloud copy without touching the network; None when sync never ran."""
    folder = directory(db)
    path = folder / "events.sqlite3"
    if not path.is_file():
        return None
    try:
        c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)  # read-only: a report never writes here
        try:
            return freshness(c)
        finally:
            c.close()
    except (OSError, sqlite3.Error):
        return None  # a damaged store only hides the cloud line; the next sync-cloud repairs or rebuilds it


