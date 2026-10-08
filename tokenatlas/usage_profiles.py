"""Small, local-only usage profile configuration for report labels.

The profile file contains display preferences only.  It is deliberately kept
next to the history database, written atomically, and never reads credentials
or provider files.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


KNOWN_LOCAL_PROVIDERS = frozenset({"ollama", "llama-swap", "lmstudio", "vllm", "local"})
KNOWN_PLAN_NAMES = frozenset({"free", "plus", "pro", "prolite", "team", "enterprise", "max", "max-5x", "max-20x"})
CLIENTS = {
    "claude_cli": "Claude CLI",
    "claude_desktop": "Claude Desktop",
    "claude_sdk": "Claude SDK",
    "local_agent": "Local agent",
    "codex_cli": "Codex CLI",
    "codex_app": "Codex App",
    "codex_exec": "Codex exec",
    "codex_work": "Codex Work",
    "pi": "Pi",
    "opencode": "OpenCode",
    "local": "Local provider",
    "unknown": "Unknown client",
}


def path_for(db_path: str | os.PathLike[str]) -> Path:
    return Path(db_path).expanduser().with_name("usage-profile.json")


def _valid_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 120 and all(ch.isprintable() and ch not in "\x7f" for ch in value) else None


def _clean(data: object) -> dict:
    if not isinstance(data, dict):
        return {"plans": {}, "local_providers": []}
    plans = data.get("plans") if isinstance(data.get("plans"), dict) else {}
    local = data.get("local_providers") if isinstance(data.get("local_providers"), list) else []
    plans = {h: _valid_name(v) for h, v in plans.items() if h in ("claude", "codex") and _valid_name(v)}
    local = sorted({_valid_name(v) for v in local if _valid_name(v)})
    return {"plans": plans, "local_providers": local}


def load(db_path: str | os.PathLike[str]) -> dict:
    path = path_for(db_path)
    try:
        st = path.lstat()
        owner_bad = os.name != "nt" and st.st_uid != os.getuid()
        mode_bad = os.name != "nt" and st.st_mode & 0o077
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or owner_bad or mode_bad:
            return {"plans": {}, "local_providers": []}
        return _clean(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {"plans": {}, "local_providers": []}


def save(db_path: str | os.PathLike[str], data: object) -> dict:
    path = path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        try:
            st = path.lstat()
            owner_bad = os.name != "nt" and st.st_uid != os.getuid()
            mode_bad = os.name != "nt" and st.st_mode & 0o077
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or owner_bad or mode_bad:
                raise ValueError(f"refusing to replace unsafe usage profile {path}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or ("plans" in raw and not isinstance(raw["plans"], dict)) or ("local_providers" in raw and not isinstance(raw["local_providers"], list)):
                raise ValueError(f"refusing to replace malformed usage profile {path}")
        except ValueError:
            raise
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"refusing to replace unreadable usage profile {path}: {exc}") from exc
    clean = _clean(data)
    fd, temporary = tempfile.mkstemp(prefix=".usage-profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return clean


def client_key(harness: object, origin: object) -> str:
    if harness == "claude":
        return {"cli": "claude_cli", "claude-desktop": "claude_desktop", "sdk-cli": "claude_sdk", "sdk-py": "claude_sdk", "sdk-ts": "claude_sdk",
                "local-agent": "local_agent", "local_agent": "local_agent"}.get(origin, "unknown")
    if harness == "codex":
        if origin in ("codex_exec",):
            return "codex_exec"
        if origin in ("Codex Desktop", "codex_work_desktop"):
            return "codex_work" if origin == "codex_work_desktop" else "codex_app"
        if origin in ("codex-tui", "codex_cli_rs", "cli"):
            return "codex_cli"
        return "unknown"
    if harness == "pi":
        return "pi"
    if harness == "opencode":
        return "opencode"
    return "unknown"


def is_local_provider(provider: object, configured: object = ()) -> bool:
    if not isinstance(provider, str):
        return False
    name = provider.strip().lower()
    if name in KNOWN_LOCAL_PROVIDERS or name.startswith("local-"):
        return True
    return name in {str(x).strip().lower() for x in (configured or ()) if isinstance(x, str)}
