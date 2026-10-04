"""Opt-in turn context: best-effort, local-only facts about the turn behind a ranked prompt.

One output shape for every harness (see `turn_context`). Every field is best effort: None/empty
when unknown, and nothing here raises. Reads local logs/DBs and, for commits, local git only:
no network, no model calls. Command text and tool output are scanned, never stored.
Activity and outcomes cover the parent turn only; child agents' files in `sources` are skipped.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .prompt_text import (
    _blocks_text, _codex_genuine, _codex_prompt, _codex_text, _json_rows, _strip_claude_wrappers, sanitize,
)
from .why import (
    _CODEX_TURN_END, _codex_event_turn_id, _codex_user_event_identity, _explicit_turn_id, _first_text,
    _is_genuine_user_row, _mapping, _meta_text, codex_session_index, parse_iso_timestamp,
)

_ERRORS = (OSError, OverflowError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, sqlite3.Error)
_PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\s+#?(\d+)")
_SPAWN = re.compile(r"spawn_agent\s*\(")
_PR_REF = re.compile(r"(?:\bPR\s*#|/pull/)(\d+)", re.I)
_CATEGORY = {  # tool name -> activity category; Codex exec is classified by its input instead
    "claude": {"Bash": "shell", **dict.fromkeys(("Edit", "Write", "MultiEdit", "NotebookEdit"), "edits"),
               "WebFetch": "web", "WebSearch": "web", "Agent": "subagents", "Task": "subagents"},
    "opencode": {"bash": "shell", **dict.fromkeys(("apply_patch", "edit", "write"), "edits"),
                 "webfetch": "web", "websearch": "web", "task": "subagents"},
    "pi": {"bash": "shell", "edit": "edits", "write": "edits", "web_search": "web", "fetch_content": "web",
           "get_search_content": "web", "subagent": "subagents"},
}
_CODEX_SHELL = frozenset({"exec", "exec_command", "shell", "shell_command", "local_shell"})
_CODEX_ITEM_USER = frozenset({"UserMessage", "user_message", "userMessage"})


def _empty() -> dict:
    return {"title": None, "title_source": None, "cwd": None, "branch": None, "repository": None,
            "inputs": {"count": None, "first": None, "followups": []},
            "final": None, "activity": {"shell": None, "edits": None, "web": None, "subagents": None},
            "outcomes": {"prs": [], "commits": []}}


def _raw() -> dict:
    """Unsanitized facts a harness reader fills; `_finish` turns them into the public shape."""
    return {"title": None, "title_source": None, "cwd": None, "branch": None, "repo": None, "inputs": [],
            "count": None, "final": None, "act": None, "cmds": [], "start": None, "end": None}


def _repo_url(value: object) -> str | None:
    """Repository URL without credentials, query or fragment."""
    value = _meta_text(value, limit=512)
    if value is None:
        return None
    if "://" not in value:
        return value.split("?")[0].split("#")[0]
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if not host:
            return None
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme}://{host}{':' + str(parts.port) if parts.port else ''}{parts.path}"
    except ValueError:
        return None


def _dedupe(inputs: list[tuple[str, str]]) -> list[str]:
    """Collapse representations of one input: per text, the largest per-kind count, in first-seen order."""
    order, seen = [], {}
    for kind, text in inputs:
        key = " ".join(text.split())
        if key not in seen:
            order.append(key)
        seen.setdefault(key, {}).setdefault(kind, 0)
        seen[key][kind] += 1
    return [key for key in order for _ in range(max(seen[key].values()))]


def _prs(commands: list[str], final: str | None) -> list[str]:
    found = []
    for text in commands:
        if _PR_CREATE.search(text):
            found.append("created")
        found += ["#" + n for n in _PR_MERGE.findall(text)]
    found += ["#" + n for n in _PR_REF.findall(final or "")]
    return list(dict.fromkeys(found))[:5]


def _stamp(value: object) -> str | None:
    found = parse_iso_timestamp(value)
    return found.isoformat() if found else None


def _span(raw: dict, *stamps: object) -> None:
    for found in sorted(s for s in map(_stamp, stamps) if s):
        raw["start"] = min(raw["start"] or found, found)
        raw["end"] = max(raw["end"] or found, found)


def _tools(raw: dict, harness: str, names_inputs: list[tuple[object, object]]) -> None:
    """Count tool calls per category; shell/Bash command text only feeds PR detection."""
    counts = {"shell": 0, "edits": 0, "web": 0, "subagents": 0}
    table = _CATEGORY[harness]
    for name, command in names_inputs:
        category = table.get(name) if isinstance(name, str) else None
        if category:
            counts[category] += 1
        if category == "shell" and isinstance(command, str):
            raw["cmds"].append(command)
    raw["act"] = counts


def _first_file(sources: object, parent_only: bool = True):
    for source in sources if isinstance(sources, (list, tuple, set)) else [sources]:
        if isinstance(source, (str, Path)) and Path(source).is_file() and not (
                parent_only and "subagents" in Path(source).parts):
            yield Path(source)


def _blocks(message: dict, row: dict) -> list:
    content = message.get("content", row.get("content"))
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _claude(sources: object, session: str, turn_id: str) -> dict | None:
    for path in _first_file(sources):
        rows = list(_json_rows(path))
        starts = [i for i, r in enumerate(rows) if _is_genuine_user_row(r) and _first_text(r, "uuid", "id") == turn_id]
        starts = starts or [i for i, r in enumerate(rows)
                            if _is_genuine_user_row(r) and _explicit_turn_id(r, _mapping(r.get("message"))) == turn_id]
        if not starts:
            continue
        raw, begin = _raw(), starts[0]
        end = next((i for i in range(begin + 1, len(rows))
                    if _is_genuine_user_row(rows[i]) and not rows[i].get("isSidechain")), len(rows))
        head = rows[begin]
        titles = [r.get("customTitle") for r in rows if r.get("type") == "custom-title"]
        raw["title"], raw["title_source"] = (titles[-1], "custom-title") if titles and titles[-1] else (None, None)
        body = [r for r in rows[begin + 1:end] if not r.get("isSidechain")]
        message = _mapping(head.get("message"))
        raw["inputs"] = [_strip_claude_wrappers(_blocks_text(message.get("content", head.get("content"))))]
        raw["count"] = 1
        calls = {}
        for row in [head, *body]:
            raw["cwd"] = raw["cwd"] or _meta_text(row.get("cwd"), limit=4096)
            raw["branch"] = raw["branch"] or _meta_text(row.get("gitBranch"), limit=256)
            _span(raw, row.get("timestamp"))
            if row.get("type") != "assistant":
                continue
            for block in _blocks(_mapping(row.get("message")), row):
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    raw["final"] = block["text"]
                elif block.get("type") == "tool_use":
                    calls[block.get("id") or id(block)] = (block.get("name"), _mapping(block.get("input")).get("command"))
        _tools(raw, "claude", list(calls.values()))
        return raw
    return None


def _codex_item_text(payload: dict) -> str:
    item = _mapping(payload.get("item"))
    if item.get("type") not in _CODEX_ITEM_USER:
        return ""
    return _codex_prompt(_blocks_text(item.get("content")) or _first_text(item, "text", "message") or "")


def _codex_replay(rows: list[dict], turn_id: str) -> tuple[list[tuple[str, str]], list[dict]] | None:
    """Replay the extract_prompt/collect_codex turn assignment: (kind, text) inputs and the turn's rows."""
    cur, closed, found = None, False, False
    buf: list[tuple[str, str]] = []
    inputs: list[tuple[str, str]] = []
    body: list[dict] = []
    for row in rows:
        payload = _mapping(row.get("payload"))
        row_type, event_type = row.get("type"), payload.get("type")
        if row_type == "session_meta":
            continue
        explicit = _meta_text(_codex_event_turn_id(row, payload))
        genuine = _codex_genuine(row_type, event_type, payload)
        kind = "event" if row_type == "event_msg" else "resp"
        text = _codex_prompt(_codex_text(row, payload)) if genuine else ""
        if event_type == "item_completed":
            kind, text = "item", _codex_item_text(payload)
        ended = row_type == "event_msg" and event_type in _CODEX_TURN_END
        if explicit:
            if explicit != cur:
                cur, closed = explicit, False
                if cur == turn_id:
                    found = True
                    inputs += [] if text else buf[-1:]
            elif cur == turn_id:
                inputs += buf
            buf = []
            if cur == turn_id and text:
                inputs.append((kind, text))
        elif genuine:
            if text:
                buf.append((kind, text))
            continue
        if cur == turn_id and not closed:
            body.append(row)
        closed = closed or ended
    if cur == turn_id and not closed:
        inputs += buf
    return (inputs, body) if found else None


def _codex_legacy(rows: list[dict], turn_id: str) -> tuple[list[tuple[str, str]], list[dict]] | None:
    """Turn ids derived from a user event's own id: the turn runs to the next genuine user event."""
    inputs, body, active = [], [], False
    for row in rows:
        payload = _mapping(row.get("payload"))
        genuine = _codex_genuine(row.get("type"), payload.get("type"), payload)
        text = _codex_prompt(_codex_text(row, payload)) if genuine else ""
        if text:
            if active:
                break
            if _meta_text(_codex_user_event_identity(row, payload)) == turn_id:
                active, inputs = True, [("event", text)]
                continue
        if active and not _meta_text(_codex_event_turn_id(row, payload)):
            body.append(row)
    return (inputs, body) if active else None


def _codex_title(index: Path, session: str) -> str | None:
    best = (None, None)
    for row in _json_rows(index):
        if row.get("id") == session and isinstance(row.get("thread_name"), str) and row["thread_name"].strip():
            stamp = str(row.get("updated_at") or "")
            if best[0] is None or stamp >= best[0]:
                best = (stamp, row["thread_name"])
    return best[1]


def _codex(sources: object, session: str, turn_id: str, index: Path) -> dict | None:
    candidates = []
    for path in _first_file(sources):
        rows = list(_json_rows(path))
        meta = next((_mapping(r.get("payload")) for r in rows if r.get("type") == "session_meta"), {})
        found = _codex_replay(rows, turn_id) or _codex_legacy(rows, turn_id)
        if found:
            candidates.append((meta.get("id") == session, meta, found, path))
    if not candidates:
        return None
    _, meta, (inputs, body), _path = max(candidates, key=lambda c: c[0])
    raw = _raw()
    raw["title"] = _codex_title(index, session)
    raw["title_source"] = "thread_name" if raw["title"] else None
    git = _mapping(meta.get("git"))
    raw["cwd"], raw["branch"] = _meta_text(meta.get("cwd"), limit=4096), _meta_text(git.get("branch"), limit=256)
    raw["repo"] = git.get("repository_url")
    items, seen = [], set()  # UserMessage items are the reliable owner inputs; role=user rows also carry agent traffic
    for row in body:
        payload = _mapping(row.get("payload"))
        text = _codex_item_text(payload) if payload.get("type") == "item_completed" else ""
        item = _mapping(payload.get("item"))
        key = _meta_text(item.get("id")) or " ".join(text.split())
        if text and key not in seen:
            seen.add(key)
            items.append(text)
    if items:
        head = inputs[0][1] if inputs else None
        norm = lambda t: " ".join(t.split())
        lead = [head] if head and norm(head) not in {norm(t) for t in items} else []
        texts = lead + items
        raw["inputs"], raw["count"] = texts, len(texts)
    else:
        texts = _dedupe(inputs)
        raw["inputs"], raw["count"] = texts, len(texts) or None
    counts = {"shell": 0, "edits": 0, "web": 0, "subagents": 0}
    web_items = 0
    for row in body:
        payload = _mapping(row.get("payload"))
        row_type, kind = row.get("type"), payload.get("type")
        _span(raw, row.get("timestamp"))
        if row_type == "turn_context":
            raw["cwd"] = _meta_text(payload.get("cwd"), default=raw["cwd"], limit=4096)
        if row_type == "response_item" and kind == "message" and payload.get("role") == "assistant":
            text = "\n".join(b["text"] for b in _blocks(payload, {}) if isinstance(b.get("text"), str))
            raw["final"] = text if text.strip() else raw["final"]
        elif row_type == "response_item" and kind == "web_search_call":
            counts["web"] += 1
        elif row_type == "event_msg" and kind == "item_completed" \
                and _mapping(payload.get("item")).get("type") in {"WebSearch", "web_search"}:
            web_items += 1
        elif row_type == "response_item" and kind in {"function_call", "custom_tool_call", "local_shell_call"}:
            name = payload.get("name") or ("local_shell" if kind == "local_shell_call" else None)
            args = payload.get("arguments") or payload.get("input") or json.dumps(payload.get("action") or "")
            args = args if isinstance(args, str) else json.dumps(args)
            if name in _CODEX_SHELL:
                if "apply_patch" in args:
                    counts["edits"] += 1
                else:
                    counts["shell"] += 1
                counts["subagents"] += len(_SPAWN.findall(args))
                raw["cmds"].append(args)
            elif name in {"apply_patch", "patch"}:
                counts["edits"] += 1
            elif isinstance(name, str) and (name.startswith("spawn") or "spawn_agent" in name):
                counts["subagents"] += 1
            elif isinstance(name, str) and name in {"web_search", "web.run", "web_fetch"}:
                counts["web"] += 1
    counts["web"] = max(counts["web"], web_items)
    raw["act"] = counts
    return raw


def _load(text: object) -> dict:
    value = json.loads(text) if text else {}
    return value if isinstance(value, dict) else {}


def _opencode(sources: object, session: str, turn_id: str) -> dict | None:
    for path in _first_file(sources, parent_only=False):
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        except (sqlite3.Error, OSError):
            continue
        try:
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            head = db.execute("SELECT data, time_created FROM message WHERE id=? AND session_id=?",
                              (turn_id, session)).fetchone()
            if head is None:
                continue
            ses = db.execute("SELECT * FROM session WHERE id=?", (session,)).fetchone()
            msgs = db.execute("SELECT id, data, time_created, time_updated FROM message WHERE session_id=? "
                              "ORDER BY time_created, id", (session,)).fetchall()
            parts = db.execute("SELECT message_id, data FROM part WHERE session_id=? ORDER BY time_created, id",
                               (session,)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            db.close()
        kids = {}
        for m in msgs:
            try:
                data = _load(m["data"])
            except ValueError:
                continue
            if data.get("role") == "assistant" and _first_text(data, "parentID", "parentId") == turn_id:
                kids[m["id"]] = (data, m)
        by_message = {}
        for p in parts:
            try:
                by_message.setdefault(p["message_id"], []).append(_load(p["data"]))
            except ValueError:
                continue
        raw = _raw()
        if ses is not None:
            keys = ses.keys()
            raw["title"] = ses["title"] if "title" in keys and isinstance(ses["title"], str) and ses["title"].strip() else None
            raw["title_source"] = "session.title" if raw["title"] else None
            raw["cwd"] = _meta_text(ses["directory"] if "directory" in keys else None, limit=4096)
        raw["inputs"] = ["\n".join(d["text"] for d in by_message.get(turn_id, []) if d.get("type") == "text"
                                   and isinstance(d.get("text"), str) and not d.get("synthetic"))]
        raw["count"] = 1
        calls, ms = [], [head["time_created"]]
        for mid, (data, m) in kids.items():
            ms += [m["time_created"], m["time_updated"], _mapping(data.get("time")).get("completed")]
            tools = [p for p in by_message.get(mid, []) if p.get("type") == "tool"]
            calls += [(p.get("tool"), _mapping(_mapping(p.get("state")).get("input")).get("command")) for p in tools]
            if any(p.get("type") == "patch" for p in by_message.get(mid, [])) \
                    and not any(_CATEGORY["opencode"].get(p.get("tool")) == "edits" for p in tools):
                calls.append(("edit", None))
            for p in by_message.get(mid, []):
                if p.get("type") == "text" and isinstance(p.get("text"), str) and p["text"].strip() \
                        and not p.get("synthetic"):
                    raw["final"] = p["text"]
        _tools(raw, "opencode", calls)
        ms = [v for v in ms if isinstance(v, (int, float)) and not isinstance(v, bool)]
        try:
            if ms:
                raw["start"], raw["end"] = (datetime.fromtimestamp(f(ms) / 1000, tz=timezone.utc).isoformat() for f in (min, max))
        except (OverflowError, OSError, ValueError):
            raw["start"] = raw["end"] = None  # an extreme or invalid stamp only loses the commit window
        return raw
    return None


def _pi(sources: object, session: str, turn_id: str) -> dict | None:
    for path in _first_file(sources):
        rows = list(_json_rows(path))
        begin = next((i for i, r in enumerate(rows) if r.get("id") == turn_id and r.get("type", "message") == "message"
                      and _mapping(r.get("message")).get("role") == "user"), None)
        if begin is None:
            continue
        raw = _raw()
        header = next((r for r in rows if r.get("type") == "session"), {})
        raw["cwd"] = _meta_text(header.get("cwd"), limit=4096)
        names = [r.get("name") for r in rows if r.get("type") == "session_info" and isinstance(r.get("name"), str)
                 and r["name"].strip()]
        raw["title"], raw["title_source"] = (names[-1], "session_info") if names else (None, None)
        end = next((i for i in range(begin + 1, len(rows))
                    if rows[i].get("type") == "message" and _is_genuine_user_row(rows[i])), len(rows))
        head = _mapping(rows[begin].get("message"))
        raw["inputs"], raw["count"] = [_blocks_text(head.get("content"))], 1
        calls = []
        for row in rows[begin:end]:
            _span(raw, row.get("timestamp"))
            message = _mapping(row.get("message"))
            if row.get("type") != "message" or message.get("role") != "assistant":
                continue
            for block in _blocks(message, row):
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    raw["final"] = block["text"]
                elif block.get("type") == "toolCall":
                    calls.append((block.get("name"), _mapping(block.get("arguments")).get("command")))
        _tools(raw, "pi", calls)
        return raw
    return None


# The project directory is untrusted: its .git/config is repository-local and can name programs. `git log
# --format=%s` can reach these callbacks, each neutralised below (an empty/`false`/`/dev/null` value makes git fail
# closed or do nothing; it never runs a program from the repository):
#   log.showSignature -> gpg.program / gpg.openpgp.program / gpg.ssh.program / gpg.x509.program (signature verify)
#   core.pager / pager.log / GIT_PAGER (pager, normally off without a tty)
#   diff.external / diff.<driver>.command / diff.<driver>.textconv (only with patch output; --format never shows it)
#   core.fsmonitor (hook or daemon run on index refresh)
#   core.alternateRefsCommand (external-object alternates: `--all` reads alternate refs)
#   lazy fetch of promisor objects -> core.sshCommand, core.gitProxy, credential.helper, remote.<n>.uploadpack,
#     url.<base>.insteadOf transports, GIT_ASKPASS (blocked by protocol.allow=never and GIT_NO_LAZY_FETCH)
#   core.hooksPath (git log runs no hooks; defence in depth)
# Not reachable by log: gpg.ssh.defaultKeyCommand (signing only), filter.<driver>.* (checkout), core.editor.
_GIT_SAFE_CONFIG = [
    arg for pair in (
        "core.fsmonitor=false", "log.showSignature=false", "gpg.program=false", "gpg.openpgp.program=false",
        "gpg.ssh.program=false", "gpg.x509.program=false", "core.pager=cat", "pager.log=false", "diff.external=",
        "core.sshCommand=false", "protocol.allow=never", "core.hooksPath=/dev/null", "core.alternateRefsCommand=",
        "core.alternateRefsPrefixes=", "core.askPass=false", "core.gitProxy=false", "credential.helper=",
        "protocol.ext.allow=never", "protocol.file.allow=never", "protocol.http.allow=never",
        "protocol.https.allow=never", "protocol.git.allow=never", "protocol.ssh.allow=never",
    ) for arg in ("-c", pair)]


def _git_env() -> dict:
    """Allowlisted environment. GIT_ALLOW_PROTOCOL is an environment allowlist that repository config
    (`protocol.<name>.allow=always`) cannot override; empty means no transport, so no `ext::` program."""
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_CONFIG_NOSYSTEM="1",
               GIT_PAGER="cat", GIT_ASKPASS="false", GIT_SSH_COMMAND="false", GIT_ALLOW_PROTOCOL="")
    return env  # GIT_EXTERNAL_DIFF and other GIT_* variables are not inherited


def git_commits(cwd: object, start: object, end: object) -> list[str]:
    """Up to 5 commit subjects between start and end (datetime or ISO text) from local git; [] on any error."""
    try:
        since, until = (v.isoformat() if isinstance(v, datetime) else v for v in (start, end))
        if not (isinstance(cwd, (str, Path)) and Path(cwd).is_dir() and isinstance(since, str) and isinstance(until, str)):
            return []
        done = subprocess.run(
            ["git", *_GIT_SAFE_CONFIG, "--no-pager", "-C", str(cwd), "log", "--all", "--no-show-signature", "--no-ext-diff",
             "--no-textconv", f"--since={since}", f"--until={until}", "--format=%s", "-n", "5"],
            capture_output=True, text=True, timeout=5, env=_git_env(), stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            return []
        return [s for s in (sanitize(line, 100) for line in done.stdout.splitlines()) if s][:5]
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return []


def _finish(raw: dict, limit: int, start: object, end: object) -> dict:
    out = _empty()
    title = sanitize(raw["title"], 200)
    out["title"], out["title_source"] = title, raw["title_source"] if title else None
    cwd = _meta_text(raw["cwd"], limit=4096)  # raw: only the local git lookup below uses it
    out["cwd"] = sanitize(cwd, 4096)
    out["branch"] = sanitize(_meta_text(raw["branch"], limit=256), 256)
    out["repository"] = sanitize(_repo_url(raw["repo"]), 512)
    texts = [t for t in raw["inputs"] if isinstance(t, str)]
    out["inputs"] = {"count": raw["count"], "first": sanitize(texts[0], 200) if texts else None,
                     "followups": [s for s in (sanitize(t, 100) for t in texts[1:6]) if s]}
    out["final"] = sanitize(raw["final"], limit)
    out["activity"] = dict(raw["act"] or out["activity"])
    out["outcomes"]["prs"] = _prs(raw["cmds"], raw["final"])
    out["outcomes"]["commits"] = git_commits(cwd, raw["start"] or start, raw["end"] or end)
    return out


def turn_context(harness: str, sources: object, session: object, turn_id: object, start: object = None,
                 end: object = None, limit: int = 400, codex_index: str | Path | None = None) -> dict:
    """Best-effort context of the parent turn `(harness, session, turn_id)`; unknown fields are None/empty."""
    try:
        if not isinstance(turn_id, str) or not turn_id or not isinstance(session, str):
            return _empty()
        if harness == "claude":
            raw = _claude(sources, session, turn_id)
        elif harness == "codex":
            raw = _codex(sources, session, turn_id, Path(codex_index) if codex_index else codex_session_index())
        elif harness == "opencode":
            raw = _opencode(sources, session, turn_id)
        elif harness == "pi":
            raw = _pi(sources, session, turn_id)
        else:
            raw = None
        return _finish(raw, limit, start, end) if raw else _empty()
    except _ERRORS:
        return _empty()
