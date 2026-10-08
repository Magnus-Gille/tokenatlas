"""Install and inspect a TokenAtlas ``collect`` schedule.

The scheduler files and entries written here carry an explicit ownership marker.  That
marker is required before an entry is replaced or removed; a user's unrelated cron,
launchd, systemd, or Task Scheduler configuration is left alone.
"""

from __future__ import annotations

import json
import csv
import io
import xml.etree.ElementTree as ET
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


MARKER = "tokenatlas schedule"
LABEL = "com.tokenatlas.collect"
SYSTEMD_NAME = "tokenatlas-collect"
WINDOWS_NAME = "TokenAtlas\\Collect"
HARNESS_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "PI_CODING_AGENT_DIR")
_DURATION = re.compile(r"^(?P<n>[1-9][0-9]*)(?P<u>[smhd])$")
_TAG = re.compile(r"^[A-Za-z0-9_-]+$")
_HOST = re.compile(r"^[A-Za-z0-9._@:-]+$")


class ScheduleError(ValueError):
    """A schedule request cannot be represented safely."""


class ScheduleUnavailable(ScheduleError):
    """An optional scheduler executable is missing."""


def _home() -> Path:
    return Path.home()


def _platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    return "linux"


def parse_every(text: str) -> int:
    """Return an interval in seconds, accepting e.g. ``30m`` or ``2h``."""
    found = _DURATION.fullmatch(text)
    if not found:
        raise ScheduleError("--every must be a positive duration such as 30m, 1h or 1d")
    seconds = int(found["n"]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[found["u"]]
    if seconds > 2147483647:
        raise ScheduleError("--every exceeds the supported scheduler interval")
    return seconds


def _remote_pairs(values: Iterable[str]) -> list[str]:
    pairs = []
    for raw in values:
        if not isinstance(raw, str) or "\x00" in raw or "\r" in raw or "\n" in raw:
            raise ScheduleError("--remote contains an unsafe control character")
        tag, sep, host = raw.partition(":")
        if not sep or not _TAG.fullmatch(tag) or not _HOST.fullmatch(host) or host.startswith("-"):
            raise ScheduleError(f"invalid --remote {raw!r}; use TAG:HOST with a safe SSH host")
        pairs.append(raw)
    return pairs


def _executable() -> list[str]:
    """The absolute executable/argv that runs this installation."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name.lower() in ("tokenatlas", "tokenatlas.exe") and argv0.exists():
        return [str(argv0.absolute())]
    return [str(Path(sys.executable).absolute()), "-m", "tokenatlas"]


def _resolved_db(explicit: Path | str | None = None) -> Path:
    value = explicit or os.environ.get("TOKENATLAS_DB")
    if value:
        return Path(value).expanduser().absolute()
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser().absolute() if state is not None else _home() / ".local" / "state"
    if not (base / "tokenatlas").exists() and (base / "agentmon").is_dir():
        raise ScheduleError("legacy agentmon history exists; run tokenatlas doctor to migrate it before scheduling")
    return base / "tokenatlas" / "history.sqlite3"


def _path_value() -> str:
    """A deterministic non-interactive PATH containing the local tools collect uses."""
    values: list[str] = []
    executable = Path(_executable()[0])
    if executable.is_absolute() and executable.parent not in (Path("/"), Path(".")):
        values.append(str(executable.parent))
    values.extend(os.environ.get("PATH", "").split(os.pathsep))
    values.extend(("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    out = []
    for value in values:
        if value and value not in out and Path(value).is_absolute():
            if any(c in value for c in "\x00\r\n"):
                raise ScheduleError("PATH contains an unsupported control character")
            out.append(value)
    return os.pathsep.join(out)


def _harness_environment() -> dict[str, str]:
    # why.harness_root only accepts absolute values.  Match that rule here so a
    # scheduled run cannot silently resolve a relative path against an arbitrary cwd.
    result = {"PATH": _path_value()}
    for name in HARNESS_VARS:
        value = os.environ.get(name)
        if value and Path(value).is_absolute() and all(c not in value for c in "\x00\r\n"):
            result[name] = value
    return result


def _argv(remotes: list[str], db: Path | str | None = None) -> list[str]:
    prefix = ["--db", str(_resolved_db(db))] if db is not None else []
    return [*_executable(), *prefix, "collect", *sum((["--remote", pair] for pair in remotes), [])]


def _log_path(platform: str | None = None) -> Path:
    platform = platform or _platform()
    if platform == "darwin":
        return _home() / "Library" / "Logs" / "tokenatlas" / "collect.log"
    if platform == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / "tokenatlas" / "collect.log"
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state and Path(state).is_absolute() else _home() / ".local" / "state"
    return base / "tokenatlas" / "collect.log"


def _mac_paths() -> tuple[Path, Path]:
    return _home() / "Library" / "LaunchAgents" / f"{LABEL}.plist", _log_path("darwin")


def _systemd_paths() -> tuple[Path, Path, Path]:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else _home() / ".config"
    user = base / "systemd" / "user"
    return user / f"{SYSTEMD_NAME}.service", user / f"{SYSTEMD_NAME}.timer", _log_path("linux")


def _windows_paths() -> tuple[Path, Path]:
    base = Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / "tokenatlas"
    return base / "collect.py", _log_path("windows")


def _plist(interval: int, remotes: list[str], log: Path, db: Path | str | None = None) -> bytes:
    import plistlib

    env = _harness_environment()
    doc = {
        "Label": LABEL,
        "Comment": f"Managed by {MARKER}",
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProgramArguments": _argv(remotes, db),
        "EnvironmentVariables": env,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=False)


def _systemd_quote(value: str, *, exec_argument: bool = False) -> str:
    # A doubled percent is a literal percent in a systemd unit (single percent
    # starts a specifier expansion).
    value = value.replace("%", "%%")
    if exec_argument:
        value = value.replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _systemd_exec(argv: list[str]) -> str:
    return " ".join(_systemd_quote(arg, exec_argument=True) for arg in argv)


def _systemd_output(log: Path) -> str:
    value = str(log)
    if value != value.strip() or any(c in value for c in "\0\n\r"):
        raise ScheduleError("systemd log path contains unsupported whitespace")
    # StandardOutput uses its own parser, which does not unquote the value.
    return 'append:' + value.replace('%', '%%')


def _units(interval: int, remotes: list[str], log: Path, db: Path | str | None = None) -> tuple[str, str]:
    env = "\n".join(f'Environment={_systemd_quote(name + "=" + value)}' for name, value in _harness_environment().items())
    command = _systemd_exec(_argv(remotes, db))
    service = (
        f"# Managed by {MARKER}\n[Unit]\nDescription=TokenAtlas collect\n\n[Service]\nType=oneshot\n"
        f"{env}\nExecStart={command}\nStandardOutput={_systemd_output(log)}\n"
        f"StandardError={_systemd_output(log)}\n"
    )
    timer = (
        f"# Managed by {MARKER}\n[Unit]\nDescription=TokenAtlas collect timer\n\n[Timer]\n"
        f"OnBootSec=1min\nOnUnitActiveSec={interval}s\nPersistent=true\nUnit={SYSTEMD_NAME}.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def _windows_script(remotes: list[str], log: Path, db: Path | str | None = None) -> str:
    # Task Scheduler starts Python directly. No cmd.exe, batch expansion, or shell.
    argv = _argv(remotes, db)
    config = dict(argv=argv, environment=_harness_environment(), log=str(log))
    return (f"# Managed by {MARKER}\n# argv: {json.dumps(argv)}\n"
            "import json, os, subprocess, sys\n"
            f"config = json.loads({json.dumps(config)!r})\n"
            "env = dict(os.environ, **config['environment'])\n"
            "with open(config['log'], 'ab') as log:\n"
            "    result = subprocess.run(config['argv'], env=env, stdout=log, stderr=subprocess.STDOUT)\n"
            "sys.exit(result.returncode)\n")


def _windows_action(script: Path) -> list[str]:
    return [str(Path(sys.executable).absolute()), str(script)]


def _atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, mode, encoding=None if mode == "wb" else "utf-8", newline="" if mode == "w" else None) as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _owned(path: Path) -> bool:
    # A symlink at a scheduler-owned path is never ours: following it could
    # replace or delete a file outside the scheduler directory.
    if path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return MARKER in text


def _run(command: list[str], *, input_text: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        if Path(command[0]).name == 'crontab':
            result = subprocess.run(command, input=input_text.encode('utf-8', 'surrogateescape') if input_text is not None else None,
                                    capture_output=True, check=check, timeout=30)
            return subprocess.CompletedProcess(command, result.returncode, result.stdout.decode('utf-8', 'surrogateescape'), result.stderr.decode('utf-8', 'surrogateescape'))
        return subprocess.run(command, input=input_text, capture_output=True, text=True, check=check, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ScheduleError(f"{command[0]} timed out after 30 seconds") from exc


def _probe(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return _run(command)
    except (OSError, ScheduleError) as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _systemd_available() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        return _run([systemctl, "--user", "show", "--property=Version", "--value"]).returncode == 0
    except (OSError, ScheduleError):
        return False


def _systemd_available_for_plan(dry_run: bool) -> bool:
    # This is a read-only user-session probe. Dry-run must select the same
    # backend as installation; it still performs no scheduler writes.
    return _systemd_available()


def _crontab() -> tuple[str, bool]:
    command = shutil.which("crontab") or "crontab"
    try:
        result = _run([command, "-l"])
    except FileNotFoundError as exc:
        raise ScheduleUnavailable("crontab is unavailable") from exc
    except OSError as exc:
        raise ScheduleError(f"could not inspect crontab: {exc}") from exc
    if result.returncode == 0:
        return result.stdout, True
    if result.returncode == 1 and not result.stdout and not result.stderr.strip():
        return "", False
    # Implementations conventionally return 1 with “no crontab for …”.
    if result.returncode == 1 and "no crontab" in result.stderr.lower():
        return "", False
    raise ScheduleError(f"could not read crontab: {result.stderr.strip() or result.returncode}")


def _cron_expression(interval: int) -> str:
    if interval % 60:
        raise ScheduleError("this platform's crontab fallback needs an interval in whole minutes")
    minutes = interval // 60
    if minutes <= 0 or minutes > 60 or 60 % minutes:
        raise ScheduleError("crontab fallback supports minute intervals that divide one hour (for example 30m)")
    return f"*/{minutes} * * * *" if minutes < 60 else "0 * * * *"


def _cron_line(interval: int, remotes: list[str], log: Path, db: Path | str | None = None) -> str:
    command = shlex.join(_argv(remotes, db)) + " >> " + shlex.quote(str(log)) + " 2>&1"
    # cron treats an unescaped percent as a newline before the shell sees it.
    environment = " ".join(f"{name}={shlex.quote(value)}" for name, value in _harness_environment().items())
    body = environment + " " + command
    if r"\%" in body:
        raise ScheduleError("crontab cannot safely represent a backslash before a percent in a path")
    body = body.replace("%", r"\%") + f" # {MARKER}"
    if len(body.encode('utf-8')) >= 1000:
        raise ScheduleError("crontab command exceeds the portable 999-byte limit; shorten PATH or use systemd")
    return f"{_cron_expression(interval)} " + body



def _cron_owned_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.rstrip("\r").endswith(f"# {MARKER}")]


def _cron_handwritten(text: str) -> list[str]:
    return [line for line in text.split("\n") if "collect" in line and "tokenatlas" in line and f"# {MARKER}" not in line]


def _cron_install(interval: int, remotes: list[str], log: Path, dry_run: bool, db: Path | str | None = None) -> dict:
    current, _ = _crontab()
    line = _cron_line(interval, remotes, log, db)
    kept = "".join(item for item in re.findall(r"[^\n]*\n|[^\n]+$", current) if not item.rstrip("\r\n").endswith(f"# {MARKER}"))
    updated = kept + ("\n" if kept and not kept.endswith("\n") else "") + line + "\n"
    result = {"backend": "cron", "location": "crontab", "files": [], "commands": [["crontab", "-l"], ["crontab", "-"]], "argv": _argv(remotes, db), "log": str(log), "warnings": ["A hand-written TokenAtlas collect schedule may also be installed."] if _cron_handwritten(current) else []}
    if not dry_run:
        log.parent.mkdir(parents=True, exist_ok=True)
        command = shutil.which("crontab") or "crontab"
        done = _run([command, "-"], input_text=updated)
        if done.returncode:
            raise ScheduleError(f"could not install crontab: {done.stderr.strip() or done.returncode}")
    else:
        result["writes"] = []
    result["entry"] = line
    if dry_run:
        result["stdin"] = updated
    return result


def _backend(platform: str, dry_run: bool = False) -> str:
    if platform == "darwin":
        return "launchd"
    if platform == "windows":
        return "schtasks"
    return "systemd" if _systemd_available_for_plan(dry_run) else "cron"


def _plan(interval: int, remotes: list[str], backend: str, platform: str, db: Path | str | None = None) -> dict:
    log = _log_path(platform)
    if backend == "launchd":
        path, _ = _mac_paths()
        commands = [["launchctl", "bootout", f"gui/{getattr(os, 'getuid', lambda: 0)()}/{LABEL}"], ["launchctl", "bootstrap", f"gui/{getattr(os, 'getuid', lambda: 0)()}", str(path)]]
        return {"backend": backend, "files": [str(path), str(log)], "commands": commands, "argv": _argv(remotes, db), "log": str(log), "interval_seconds": interval}
    if backend == "systemd":
        service, timer, _ = _systemd_paths()
        commands = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", f"{SYSTEMD_NAME}.timer"], ["systemctl", "--user", "restart", f"{SYSTEMD_NAME}.timer"]]
        return {"backend": backend, "files": [str(service), str(timer), str(log)], "commands": commands, "argv": _argv(remotes, db), "log": str(log), "interval_seconds": interval}
    if backend == "schtasks":
        script, _ = _windows_paths()
        commands = [["schtasks", "/Create", "/TN", WINDOWS_NAME, "/TR", subprocess.list2cmdline(_windows_action(script)), "/SC", "MINUTE", "/MO", str(interval // 60), "/IT", "/F"]]
        return {"backend": backend, "files": [str(script), str(log)], "commands": commands, "argv": _argv(remotes, db), "log": str(log), "interval_seconds": interval}
    return {"backend": backend, "files": [], "commands": [], "argv": _argv(remotes, db), "log": str(log), "interval_seconds": interval}


def _launch_state(path: Path) -> str:
    result = _probe(["launchctl", "print", f"gui/{getattr(os, 'getuid', lambda: 0)()}/{LABEL}"])
    if result.returncode in (3, 113):
        return "absent"
    if result.returncode:
        return "error"
    paths = [line.strip().partition(" = ")[2] for line in result.stdout.splitlines() if line.strip().startswith("path = ")]
    return "owned" if paths == [str(path)] and _owned(path) else "foreign"


def _check_systemd_paths(service: Path, timer: Path):
    for path in (service, timer):
        result = _probe(["systemctl", "--user", "show", path.name, "--property=FragmentPath", "--value"])
        if result.returncode not in (0, 1, 4):
            raise ScheduleError("cannot inspect existing systemd unit ownership")
        existing = result.stdout.strip()
        if existing and (existing != str(path) or not _owned(path)):
            raise ScheduleError(f"refusing to modify existing systemd unit {path.name}")


def _install(interval: int, remotes: list[str], backend: str, platform: str, dry_run: bool, db: Path | str | None = None) -> dict:
    if backend == "launchd":
        path, log = _mac_paths()
        if os.path.lexists(path) and not _owned(path):
            raise ScheduleError(f"refusing to replace existing non-TokenAtlas file: {path}")
        state = _launch_state(path)
        if state in ("foreign", "error"):
            raise ScheduleError("cannot establish ownership of existing launchd service")
        previous = path.read_bytes() if path.is_file() else None
        result = _plan(interval, remotes, backend, platform, db)
        if state == "absent":
            result['commands'] = result['commands'][1:]
        if dry_run:
            result["writes"] = []
            result["content"] = _plist(interval, remotes, log, db).decode()
            return result
        log.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, _plist(interval, remotes, log, db))
        uid = getattr(os, 'getuid', lambda: 0)()
        if state == "owned":
            try:
                _checked(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
            except (OSError, ScheduleError):
                _atomic_write(path, previous)
                raise
        done = _probe(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if done.returncode:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, previous)
                if state == "owned":
                    restored = _probe(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
                    if restored.returncode:
                        raise ScheduleError("launchctl bootstrap failed; previous file restored but restart failed")
            raise ScheduleError(f"launchctl bootstrap failed: {done.stderr.strip() or done.returncode}")
        result["writes"] = [str(path)]
        result["warnings"] = []
        return result
    if backend == "systemd":
        service, timer, log = _systemd_paths()
        if any(os.path.lexists(path) and not _owned(path) for path in (service, timer)):
            raise ScheduleError("refusing to replace an existing non-TokenAtlas systemd unit")
        _check_systemd_paths(service, timer)
        service_text, timer_text = _units(interval, remotes, log, db)
        previous = {path: path.read_bytes() for path in (service, timer) if path.is_file()}
        was_enabled = was_active = False
        if previous:
            was_enabled = _probe(["systemctl", "--user", "is-enabled", timer.name]).stdout.strip() == "enabled"
            was_active = _probe(["systemctl", "--user", "is-active", timer.name]).stdout.strip() == "active"
        result = _plan(interval, remotes, backend, platform, db)
        if dry_run:
            result["writes"] = []
            result["content"] = {str(service): service_text, str(timer): timer_text}
            return result
        log.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(service, service_text)
        _atomic_write(timer, timer_text)
        reload_result = _probe(["systemctl", "--user", "daemon-reload"])
        if reload_result.returncode:
            for path in (service, timer):
                if path in previous:
                    _atomic_write(path, previous[path])
                else:
                    path.unlink(missing_ok=True)
            _probe(["systemctl", "--user", "daemon-reload"])
            raise ScheduleError(f"systemd daemon-reload failed: {reload_result.stderr.strip() or reload_result.returncode}")
        done = _probe(["systemctl", "--user", "enable", f"{SYSTEMD_NAME}.timer"])
        if done.returncode == 0:
            done = _probe(["systemctl", "--user", "restart", f"{SYSTEMD_NAME}.timer"])
        if done.returncode:
            restored = []
            if not was_enabled:
                restored.append(_probe(["systemctl", "--user", "disable", "--now", timer.name]))
            for path in (service, timer):
                if path in previous:
                    _atomic_write(path, previous[path])
                else:
                    path.unlink(missing_ok=True)
            _probe(["systemctl", "--user", "daemon-reload"])
            if was_active:
                restored.append(_probe(["systemctl", "--user", "restart", timer.name]))
            if any(item.returncode for item in restored):
                raise ScheduleError("systemd update failed; configuration restored but scheduler rollback failed")
            raise ScheduleError(f"systemd enable/restart failed: {done.stderr.strip() or done.returncode}")
        result["writes"] = [str(service), str(timer)]
        result["warnings"] = []
        return result
    if backend == "schtasks":
        script, log = _windows_paths()
        if os.path.lexists(script) and not _owned(script):
            raise ScheduleError(f"refusing to replace existing non-TokenAtlas file: {script}")
        if _task_state(script) in ("foreign", "error"):
            raise ScheduleError(f"refusing to replace an existing non-TokenAtlas task: {WINDOWS_NAME}")
        if interval % 60 or not 1 <= interval // 60 <= 1439:
            raise ScheduleError("schtasks supports intervals of 1–1439 whole minutes")
        previous = script.read_bytes() if script.is_file() else None
        result = _plan(interval, remotes, backend, platform, db)
        if dry_run:
            result["writes"] = []
            result["content"] = _windows_script(remotes, log, db)
            return result
        log.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(script, _windows_script(remotes, log, db))
        done = _probe(["schtasks", "/Create", "/TN", WINDOWS_NAME, "/TR", subprocess.list2cmdline(_windows_action(script)), "/SC", "MINUTE", "/MO", str(interval // 60), "/IT", "/F"])
        if done.returncode:
            if previous is None:
                script.unlink(missing_ok=True)
            else:
                _atomic_write(script, previous)
            raise ScheduleError(f"schtasks create failed: {done.stderr.strip() or done.returncode}")
        result["writes"] = [str(script)]
        result["warnings"] = []
        return result
    return _cron_install(interval, remotes, log=_log_path("linux"), dry_run=dry_run, db=db)


def _read_log(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2); size = stream.tell()
            stream.seek(max(0, size - 65536))
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    return next((line for line in reversed(lines) if line.strip()), None)


def _shell_target(line: str) -> list[str] | None:
    """Extract the executable argv from a marked cron command."""
    try:
        tokens = shlex.split(line.replace(r"\%", "%"))
    except ValueError:
        return None
    tokens = tokens[5:]  # minute, hour, day, month, weekday
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    return tokens[:next((i for i, token in enumerate(tokens) if token in (">>", ">")), len(tokens))] or None



def _systemd_target(path: Path) -> list[str] | None:
    try:
        line = next((line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("ExecStart=")), None)
        return [item.replace("%%", "%").replace("$$", "$") for item in shlex.split(line[len("ExecStart=") :])] if line else None
    except (OSError, ValueError):
        return None


def _windows_target(path: Path) -> list[str] | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# argv: "):
                value = json.loads(line[len("# argv: "):])
                return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else None
    except (OSError, ValueError):
        pass
    return None


def _task_state(script: Path) -> str:
    """Locale-independent ownership check against the task's executable/arguments."""
    result = _probe(["schtasks", "/Query", "/TN", WINDOWS_NAME, "/XML"])
    if result.returncode:
        listed = _probe(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        if listed.returncode:
            return "error"
        names = [row[0].lstrip("\\") for row in csv.reader(io.StringIO(listed.stdout)) if row]
        return "error" if WINDOWS_NAME.casefold() in (n.casefold() for n in names) else "absent"
    try:
        root = ET.fromstring(result.stdout)
        actions = root.findall(".//{*}Actions/{*}Exec")
        if len(actions) != 1 or len(root.findall(".//{*}Actions/*")) != 1:
            return "foreign"
        command = actions[0].findtext("{*}Command") or ""
        args = actions[0].findtext("{*}Arguments") or ""
        expected = _windows_action(script)
        match = os.path.normcase(command.strip('"')) == os.path.normcase(expected[0]) and args.strip() == subprocess.list2cmdline(expected[1:])
        return "owned" if match and _owned(script) else "foreign"
    except (ET.ParseError, ValueError):
        return "foreign"


def _checked(command):
    result = _run(command)
    if result.returncode:
        raise ScheduleError(f"{command[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def status(platform: str | None = None) -> dict:
    platform = platform or _platform()
    found, errors, warnings = [], [], []
    if platform == "darwin":
        path, fallback = _mac_paths()
        if path.is_file() and _owned(path):
            import plistlib
            state = _launch_state(path)
            try:
                doc = plistlib.loads(path.read_bytes())
                found.append(dict(backend="launchd", location=str(path), argv=doc.get("ProgramArguments", []),
                                  log=doc.get("StandardOutPath", str(fallback)), loaded=state == "owned", state=state))
            except (OSError, ValueError, plistlib.InvalidFileException) as exc:
                errors.append(f"cannot read launchd configuration: {exc}")
            if state in ("foreign", "error"):
                errors.append("cannot establish which launchd service is loaded")
    elif platform == "windows":
        script, log = _windows_paths()
        if script.is_file() and _owned(script):
            state = _task_state(script)
            found.append(dict(backend="schtasks", location=str(script), argv=_windows_target(script), log=str(log), task_state=state))
            if state == "error": errors.append("cannot query Task Scheduler")
    else:
        service, timer, log = _systemd_paths()
        if any(_owned(path) for path in (service, timer)):
            def state_of(verb, expected):
                result = _probe(["systemctl", "--user", verb, f"{SYSTEMD_NAME}.timer"])
                value = result.stdout.strip()
                if value in expected: return value
                errors.append(f"systemd {verb}: {result.stderr.strip() or 'state unknown'}")
                return "unknown"
            enabled = state_of("is-enabled", ("enabled", "disabled", "static", "masked", "not-found"))
            active = state_of("is-active", ("active", "inactive", "failed", "activating", "deactivating"))
            try:
                saved = next((line[len('StandardOutput=append:'):] for line in service.read_text().splitlines() if line.startswith('StandardOutput=append:')), None)
                if saved: log = Path(saved.replace('%%', '%'))
            except (OSError, UnicodeError): pass
            found.append(dict(backend="systemd", location=f"{service}, {timer}", argv=_systemd_target(service), log=str(log), enabled=enabled, active=active))
    if platform != "windows":
        try:
            cron, _ = _crontab()
            if _cron_handwritten(cron): warnings.append("A hand-written TokenAtlas collect cron schedule may also be installed.")
            for line in _cron_owned_lines(cron):
                tokens = shlex.split(line.replace(r"\%", "%"))
                log = tokens[tokens.index('>>') + 1] if '>>' in tokens else str(_log_path(platform))
                found.append(dict(backend="cron", location="crontab", argv=_shell_target(line), log=log, entry=line))
        except ScheduleUnavailable: pass
        except (ScheduleError, ValueError, IndexError) as exc: errors.append(str(exc))
    for item in found:
        item["last_log_run"] = _read_log(Path(item["log"]))
    return {"installed": bool(found) if found or not errors else None, "schedules": found, "warnings": warnings, "errors": errors}


def _remove(platform: str, dry_run: bool) -> dict:
    result = {"removed": [], "commands": [], "writes": [], "warnings": []}
    cron = ("", False)
    if platform != "windows":
        try: cron = _crontab()
        except ScheduleUnavailable: pass
    if platform == "darwin":
        path, _ = _mac_paths()
        if path.exists() and _owned(path):
            state = _launch_state(path)
            if state in ("foreign", "error"):
                raise ScheduleError("cannot establish ownership of existing launchd service")
            result["commands"] = [["launchctl", "bootout", f"gui/{getattr(os, 'getuid', lambda: 0)()}/{LABEL}"]]
            if not dry_run:
                if state == "owned":
                    _checked(result["commands"][0])
                path.unlink()
            result["removed"] = [str(path)]
        return _remove_cron(result, dry_run, cron)
    if platform == "windows":
        script, _ = _windows_paths()
        if script.exists() and _owned(script):
            state = _task_state(script)
            if state == "error":
                raise ScheduleError("cannot inspect Task Scheduler ownership; nothing removed")
            if state == "foreign":
                result["warnings"].append(f"preserved non-TokenAtlas task: {WINDOWS_NAME}")
                return result
            result["commands"] = [["schtasks", "/Delete", "/TN", WINDOWS_NAME, "/F"]]
            if not dry_run:
                if state != "absent":
                    _checked(result["commands"][0])
                script.unlink()
            result["removed"] = [str(script)]
        return result
    service, timer, _ = _systemd_paths()
    owned_units = [path for path in (service, timer) if _owned(path)]
    if owned_units:
        if any(os.path.lexists(path) and not _owned(path) for path in (service, timer)):
            raise ScheduleError("refusing to remove a mixed owned/foreign systemd schedule")
        _check_systemd_paths(service, timer)
        result["commands"] = [["systemctl", "--user", "disable", "--now", f"{SYSTEMD_NAME}.timer"], ["systemctl", "--user", "daemon-reload"]]
        if not dry_run:
            _checked(result["commands"][0])
            for path in owned_units: path.unlink()
            _checked(result["commands"][1])
        result["removed"] = [str(path) for path in owned_units]
    return _remove_cron(result, dry_run, cron)


def _remove_cron(result, dry_run, cron):
    text, exists = cron
    owned = _cron_owned_lines(text)
    if owned:
        result["commands"].append(["crontab", "-"])
        result["removed"].append("crontab")
        if not dry_run:
            updated = "".join(line for line in re.findall(r"[^\n]*\n|[^\n]+$", text) if not line.rstrip("\r\n").endswith(f"# {MARKER}"))
            command = shutil.which("crontab") or "crontab"
            done = _run([command, "-"], input_text=updated)
            if done.returncode:
                raise ScheduleError(f"could not remove crontab entry: {done.stderr.strip() or done.returncode}")
    return result


def run(args) -> int:
    """CLI entry point; prints one JSON receipt and returns a process status."""
    interval = parse_every(args.every)
    remotes = _remote_pairs(args.remote)
    platform = _platform()
    if args.status:
        print(json.dumps(status(platform), indent=2, sort_keys=True))
        return 0
    if args.remove:
        print(json.dumps(_remove(platform, args.dry_run), indent=2, sort_keys=True))
        return 0
    backend = _backend(platform, args.dry_run)
    db = _resolved_db(getattr(args, "db", None))
    warnings = []
    if platform == "linux" and backend == "cron":
        service, timer, _ = _systemd_paths()
        if any(_owned(path) for path in (service, timer)):
            raise ScheduleError("a systemd schedule exists; restore the user manager and run schedule --remove before changing backend")
    if platform != "windows" and backend != "cron":
        try:
            cron, _ = _crontab()
        except ScheduleUnavailable:
            cron = ""
        if _cron_owned_lines(cron):
            raise ScheduleError("a managed cron schedule exists; run schedule --remove before changing backend")
        if _cron_handwritten(cron):
            warnings.append("Another TokenAtlas collect cron schedule may also be installed; review it to avoid duplicate runs.")
    result = _install(interval, remotes, backend, platform, args.dry_run, db)
    result['warnings'] = [*result.get('warnings', []), *warnings]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
