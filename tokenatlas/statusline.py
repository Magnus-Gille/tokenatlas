"""tokenatlas statusline: the Claude Code statusline as a packaged command, plus the small cache `refresh` writes for it.

Claude Code runs the command on every status update, so this module stays light (stdlib and tokenatlas.energy only; the history is never
imported or opened). Day, week and month totals come from statusline.json next to the history database, written by refresh; context and
quota are live from the payload on stdin. No network, no credentials. By default it also appends the 5-hour and weekly quota readings to
claude-quota.jsonl next to the history (see record_quota), so a turn can later be given a share of the limit; `--no-record-quota` or
TOKENATLAS_NO_QUOTA=1 turns that off, and `--record-quota` is accepted as a no-op. That is the only thing it ever writes, and a failure
to write never changes the status line.
"""
import argparse
import contextlib
import errno
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import energy, __version__

CACHE_NAME = 'statusline.json'
CACHE_DAYS = 31  # per-local-day buckets kept in the cache, today included
STALE_SECONDS = 45 * 60
CLASSES = ('fresh_input', 'cache_read', 'cache_write', 'output')
QUOTA_NAME = 'claude-quota.jsonl'
QUOTA_LAST = 'claude-quota.last'
QUOTA_LOCK = 'claude-quota.lock'
QUOTA_MAX_BYTES = 5 * 1024 * 1024  # prune above this size ...
QUOTA_KEEP_DAYS = 60  # ... to the last this many days
COUNTERS = CLASSES + ('mwh', 'requests', 'unweighted', 'incomplete')


def state_dir():
    """Default history directory, read-only (the one-time agentmon move stays with the history commands)."""
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'tokenatlas'


def default_db():
    """The history path the statusline uses without --db: the new state directory, or, while only the pre-rename agentmon directory exists, that one
    (the history commands move the whole directory on their next run, so a sidecar written there moves with it; never create the new directory
    first, which would stop the move)."""
    new = state_dir()
    legacy = new.with_name('agentmon')
    return (legacy if legacy.is_dir() and not new.exists() else new) / 'history.sqlite3'


def cache_path(db):
    return Path(db).expanduser().absolute().with_name(CACHE_NAME)


def quota_path(db):
    """The quota snapshot file next to the history database."""
    return Path(db).expanduser().absolute().with_name(QUOTA_NAME)


def build_cache(history, now=None):
    """Per-local-day buckets for the last 31 days from one query over ~32 days (ambiguous observations excluded), with the revision."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    first = today - timedelta(days=CACHE_DAYS - 1)
    since = datetime.combine(first - timedelta(days=1), datetime.min.time()).astimezone()  # one day of slack for the zone offset
    days = {}
    c = history.connection
    began = not c.in_transaction
    if began:c.execute('BEGIN')
    try:
        revision = history.revision
        rows = c.execute('SELECT o.ts_us,p.value,m.value,o.fresh_input,o.cache_read,o.cache_write,o.output,o.complete,o.reasoning,q.value FROM observations o'
                         ' LEFT JOIN strings p ON p.id=o.provider LEFT JOIN strings m ON m.id=o.model LEFT JOIN strings q ON q.id=o.quota'
                         ' WHERE o.ts_us>=? AND COALESCE(o.id_synthetic,0)=0', (int(since.timestamp()) * 1_000_000,)).fetchall()
    finally:
        if began:c.rollback()
    for ts_us, provider, model, *tokens, complete, reasoning, quota in rows:
        if quota and not any(tokens) and not reasoning and (json.loads(quota) or {}).get('status') in ('rejected', 'event'):continue  # a limit event (history.is_limit_event) is no request
        day = datetime.fromtimestamp(ts_us // 1_000_000).date()  # the machine's local zone
        if not first <= day <= today:continue
        bucket = days.setdefault(day.isoformat(), dict.fromkeys(COUNTERS, 0))
        counts = dict(zip(CLASSES, (t or 0 for t in tokens)))
        mult, weighted = energy.multiplier(provider, model)
        for k, v in counts.items():bucket[k] += v
        bucket['mwh'] += energy.mid_mwh(counts, mult)
        bucket['requests'] += 1
        bucket['unweighted'] += not weighted
        bucket['incomplete'] += not complete
    return dict(v=1, written_at=now.astimezone(timezone.utc).isoformat(timespec='seconds'), revision=revision, days=days)


def write_atomic(path, payload):
    """Write JSON next to its destination with mode 0600 and rename it into place."""
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')  # mkstemp creates the file 0600
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        try:os.unlink(temp)
        except OSError:pass
        raise


def refresh_cache(history):
    """Write statusline.json beside the history; reuse unchanged same-day totals without scanning history."""
    try:
        path = cache_path(history.path)
        now = datetime.now(timezone.utc)
        identity = {'token': history.revision_token, 'machine': history.machine, 'version': __version__,
                    'timezone': [os.environ.get('TZ'), list(time.tzname), time.timezone, time.daylight]}
        try:
            current = json.loads(_read_small(path))
            written = datetime.fromisoformat(current['written_at'])
            reusable = (isinstance(current, dict) and current.get('v') == 1
                        and current.get('revision') == history.revision
                        and current.get('source') == identity
                        and written.tzinfo is not None
                        and isinstance(current.get('days'), dict)
                        and written.astimezone().date() == now.astimezone().date())
        except (OSError, TypeError, ValueError, KeyError):
            reusable = False
        if reusable:
            current['written_at'] = now.isoformat(timespec='seconds')
            write_atomic(path, current)
        else:
            fresh = build_cache(history, now=now)
            fresh['source'] = identity
            write_atomic(path, fresh)
    except Exception as exc:
        print(f'warning: could not write {CACHE_NAME}: {type(exc).__name__}: {exc}', file=sys.stderr)


def _quota_window(value):
    """{used_percent, resets_at ISO UTC or None} from a payload window, or None when it has no finite used_percentage."""
    if not isinstance(value, dict):return None
    used, reset = value.get('used_percentage'), value.get('resets_at')
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):return None
    at = None
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        try:at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):pass
    return dict(used_percent=used, resets_at=at)


def quota_values(payload):
    """{'five_hour': ..., 'seven_day': ...} readings of a payload, each a window dict or None; None when the payload has neither."""
    limits = payload.get('rate_limits') if isinstance(payload, dict) else None
    if not isinstance(limits, dict):return None
    values = {k: _quota_window(limits.get(k)) for k in ('five_hour', 'seven_day')}
    return values if any(values.values()) else None


def _read_small(path):
    """The text of a small regular file, never blocking: a FIFO or other special file raises instead of waiting for a writer."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('not a regular file')
        with os.fdopen(os.dup(fd), encoding='utf-8') as f:return f.read(65536)
    finally:os.close(fd)


def file_problem(info):
    """Why a snapshot file is not private (not a regular file, foreign owner, group/other permissions), else None; POSIX only."""
    if os.name == 'nt':return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():return 'must be a regular file owned by this user'
    if info.st_mode & 0o077:return 'permissions must be 0600'
    return None


def quota_file_problem(path):
    """Why the snapshot file at `path` would not be written (symlink or not private), None when it is fine or absent."""
    try:info = os.lstat(path)
    except OSError:return None
    return 'must not be a symlink' if stat.S_ISLNK(info.st_mode) else file_problem(info)


def _open_private_append(path):
    """An append descriptor for the snapshot file: created 0600; an existing one is opened without following a symlink and the descriptor itself
    must be a private regular file (as for the outcomes file). Raises ValueError otherwise: no snapshot is recorded."""
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0)
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    nonblock = getattr(os, 'O_NONBLOCK', 0)
    try:fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | nonblock)  # a FIFO would otherwise block the open
    except OSError as exc:
        if exc.errno == errno.ELOOP:raise ValueError('snapshot file must not be a symlink') from None
        raise
    try:  # whatever happens below, the descriptor is closed unless it is returned: an open handle blocks a later replace or delete on Windows
        problem = file_problem(os.fstat(fd))
        if problem:raise ValueError(f'snapshot file {problem}')
        if nonblock:os.set_blocking(fd, True)  # POSIX only: a regular file, appends are as before
    except BaseException:
        os.close(fd)
        raise
    return fd


def _prune_quota(path, now):
    """Rewrite the file with the last QUOTA_KEEP_DAYS days when it is larger than QUOTA_MAX_BYTES (atomic replace, 0600); an unreadable line goes."""
    if path.stat().st_size <= QUOTA_MAX_BYTES:return
    cutoff = now - timedelta(days=QUOTA_KEEP_DAYS)
    kept = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                if datetime.fromisoformat(json.loads(line)['ts']) >= cutoff:kept.append(line if line.endswith('\n') else line + '\n')
            except (ValueError, KeyError, TypeError):continue
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.writelines(kept)
        os.replace(temp, path)
    except BaseException:
        try:os.unlink(temp)
        except OSError:pass
        raise


@contextlib.contextmanager
def _lock(path):
    """A non-blocking cross-process lock on `path` (flock on POSIX, msvcrt.locking on Windows): yields True when held, False when another process
    holds it or locking is unavailable. It never waits, so the statusline is never blocked."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NONBLOCK', 0), 0o600)
    except OSError:
        yield False
        return
    held = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except (OSError, ImportError):
            pass
        yield held
    finally:
        if held:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def record_quota(payload, db, now):
    """Append the payload's quota readings to claude-quota.jsonl next to the history, only when they differ from the last recorded ones (kept in
    claude-quota.last; an unreadable one counts as different). One short O_APPEND write, mode 0600, with the timestamp at full precision. The
    append, the last-value update and the prune run under one non-blocking lock (claude-quota.lock): when another process holds it, this reading
    is skipped. The file is pruned to the last 60 days above 5 MB. Returns True when a line was appended. Raises on any failure: the caller drops
    it, the status line is never affected."""
    values = quota_values(payload)
    if values is None:return False
    path = quota_path(db)
    last = path.with_name(QUOTA_LAST)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock(path.with_name(QUOTA_LOCK)) as held:
        if not held:return False
        try:
            if json.loads(_read_small(last)) == values:return False
        except (OSError, ValueError):pass
        session = payload.get('session_id')
        line = json.dumps(dict(ts=now.astimezone(timezone.utc).isoformat(), session=session if isinstance(session, str) else None, **values), sort_keys=True) + '\n'
        fd = _open_private_append(path)
        try:os.write(fd, line.encode('utf-8'))
        finally:os.close(fd)
        write_atomic(last, values)
        _prune_quota(path, now)
    return True


def settings_path():
    """Claude Code's settings file the setup snippet targets: $CLAUDE_CONFIG_DIR/settings.json, else ~/.claude/settings.json."""
    config = os.environ.get('CLAUDE_CONFIG_DIR')
    return (Path(config).expanduser() if config else Path.home() / '.claude') / 'settings.json'


NO_QUOTA_ENV = 'TOKENATLAS_NO_QUOTA'


TRUTHY = ('1', 'true', 'yes', 'on')


def recording_disabled_by_env(value=None):
    """TOKENATLAS_NO_QUOTA set to 1/true/yes/on turns quota recording off (value: a command-local assignment overriding the process environment)."""
    return (os.environ.get(NO_QUOTA_ENV, '') if value is None else value).strip().lower() in TRUTHY


def _command_state(command):
    """Classify one statusLine command: 'foreign', 'disabled', 'enabled', or 'unknown' when it is tokenatlas's but too complex to read reliably
    (shell operators or substitutions). Leading VAR=value assignments are honoured for TOKENATLAS_NO_QUOTA."""
    if not (isinstance(command, str) and 'tokenatlas' in command and 'statusline' in command):return 'foreign'
    if re.search(r'[;|&`<>\n]|\$\(', command):return 'unknown'
    try:tokens = shlex.split(command)
    except ValueError:return 'unknown'
    local = None
    for token in tokens:
        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*=(.*)$', token, re.S)
        if not match:break
        if token.startswith(NO_QUOTA_ENV + '='):local = match.group(1)
    if '--no-record-quota' in tokens or recording_disabled_by_env(local):return 'disabled'
    return 'enabled'


def recording_state():
    """'unknown' when no Claude Code settings file can be read (or the effective command is too complex to parse); 'foreign' when the effective
    statusline is not tokenatlas's (no snapshots can be recorded); 'disabled' when it carries --no-record-quota or TOKENATLAS_NO_QUOTA (in the
    command or the environment) is set; else 'enabled'. The effective command follows Claude's precedence: settings.local.json beside
    settings.json overrides it. Read only."""
    base = settings_path()
    readable = False
    command = None
    for path in (base, base.with_name('settings.local.json')):  # later overrides earlier
        try:
            line = json.loads(path.read_text(encoding='utf-8')).get('statusLine') or {}
            found = line.get('command')
        except (OSError, ValueError, AttributeError):
            continue
        readable = True
        if isinstance(found, str):command = found
    return _command_state(command) if readable else 'unknown'


def recording_configured():
    """True when the Claude Code statusline command is tokenatlas's and recording is not opted out (--no-record-quota or TOKENATLAS_NO_QUOTA);
    False when it is not tokenatlas's or recording is disabled; None (unknown) when no settings file can be read."""
    state = recording_state()
    return None if state == 'unknown' else state == 'enabled'


def tokens_text(n):
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if n >= scale:return f'{n / scale:.1f}{suffix}' if suffix != 'K' else f'{n / scale:.0f}K'
    return str(int(n))


def window_totals(days, today, length):
    """(tokens, mwh) over the last `length` local days ending today."""
    tokens = mwh = 0
    for i in range(length):
        bucket = days.get((today - timedelta(days=i)).isoformat())
        if bucket:
            tokens += sum(bucket[k] for k in CLASSES)
            mwh += bucket['mwh']
    return tokens, mwh


def totals_segments(cache, now):
    """['D:2.0M ~2 kWh', 'W:...', 'M:...'] with the cache time appended when it is older than 45 minutes."""
    today = now.astimezone().date()
    parts = [f'{label}:{tokens_text(t)} {energy.fmt(e)}'.removesuffix(' 0 mWh') if t == 0 else f'{label}:{tokens_text(t)} {energy.fmt(e)}'
             for label, length in (('D', 1), ('W', 7), ('M', 30)) for t, e in [window_totals(cache['days'], today, length)]]
    written = datetime.fromisoformat(cache['written_at'])
    if (now - written).total_seconds() > STALE_SECONDS:
        parts[-1] += f" ({written.astimezone().strftime('%H:%M')})"
    return parts


def _percent(value):
    """A finite number as a whole percent, else None (a missing, non-numeric, NaN or infinite value is left out)."""
    return f'{value:.0f}%' if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def render(payload, cache, now):
    """The one status line; a missing context or quota is omitted, a missing or unreadable cache omits the totals."""
    model = (payload.get('model') or {}).get('display_name') or '?'
    parts = [model]
    ctx = _percent((payload.get('context_window') or {}).get('used_percentage'))
    if ctx:parts.append(f'Ctx:{ctx}')
    limits = payload.get('rate_limits') or {}
    q5, q7 = (_percent((limits.get(k) or {}).get('used_percentage')) for k in ('five_hour', 'seven_day'))
    quota = [f'{label}:{q}' for label, q in (('5h', q5), ('7d', q7)) if q]
    if quota:parts.append(' '.join(quota))
    try:
        parts += totals_segments(cache, now)
    except Exception:
        pass  # no totals rather than a failed status line
    return ' | '.join(parts)


def read_cache(path):
    try:
        cache = json.loads(Path(path).read_text(encoding='utf-8'))
        return cache if isinstance(cache, dict) and isinstance(cache.get('days'), dict) else None
    except (OSError, ValueError):
        return None


def executable():
    """Absolute command that runs this tokenatlas: the running executable, else the one on PATH, else this interpreter with -m."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name.lower() in ('tokenatlas', 'tokenatlas.exe') and argv0.exists():
        return str(argv0.resolve())
    found = shutil.which('tokenatlas')
    return str(Path(found).resolve()) if found else f'{sys.executable} -m tokenatlas'


WINDOWS_UNSAFE = re.compile(r'[^\w\-.:\\/ ]')  # cmd.exe has no quoting that is safe for &, |, ^, %, ! and the like


def _quote(arg, windows=None):
    """One argument quoted for the shell that runs the statusLine command: POSIX quoting, or double quotes on Windows (valid in cmd.exe and bash)."""
    if not (os.name == 'nt' if windows is None else windows):
        return shlex.quote(arg)
    return subprocess.list2cmdline([arg])


def setup_text(db=None, command=None, windows=None):
    """The statusLine snippet for Claude Code's settings.json and where that file is; the file itself is never touched."""
    settings = settings_path()
    command = command or executable()
    windows = os.name == 'nt' if windows is None else windows
    args = ([] if ' -m ' in command else [command]) + ([str(Path(db).expanduser().absolute())] if db is not None else [])
    unsafe = windows and any(WINDOWS_UNSAFE.search(a) for a in args)
    if ' -m ' not in command:command = _quote(command, windows)
    if db is not None:command += f' --db {_quote(str(Path(db).expanduser().absolute()), windows)}'
    snippet = json.dumps({'statusLine': {'type': 'command', 'command': f"{command} statusline"}}, indent=2)
    warning = ('Warning: a path in this command contains a character that cmd.exe treats specially (&, |, ^, %, ! ...) and that no quoting makes '
               'safe; install TokenAtlas (and the database) under a plain path, or check that the command works before relying on it.\n\n') if unsafe else ''
    return (f'{warning}Add this to {settings} (merge it into the existing JSON; this command never edits the file):\n\n{snippet}\n\n'
            'Totals refresh whenever tokenatlas refresh, open or collect runs; context and quota are live.'
            f' The statusline also records the 5-hour and weekly quota readings Claude Code already shows (timestamp, session id, percentages and reset times) to {QUOTA_NAME} next to the history, '
            'local only (0600), account-wide, only while a Claude Code UI session is open. To turn that off add --no-record-quota to the command or set TOKENATLAS_NO_QUOTA=1.')


def run(argv, db=None, stdin=None, now=None):
    """Entry point: print one line and return 0, whatever happens. It records quota readings unless --no-record-quota or TOKENATLAS_NO_QUOTA=1 (record_quota); --record-quota is a no-op."""
    parser = argparse.ArgumentParser(prog='tokenatlas statusline', description='Claude Code statusline: reads its JSON payload on stdin and the cache refresh writes; no network.')
    parser.add_argument('--setup', action='store_true', help="Print the statusLine snippet for Claude Code's settings and its location, without editing it.")
    parser.add_argument('--record-quota', action='store_true', help='Accepted for compatibility; recording is on by default.')
    parser.add_argument('--no-record-quota', action='store_true', help=f'Do not append the 5-hour and weekly quota readings to {QUOTA_NAME} next to the history (also TOKENATLAS_NO_QUOTA=1).')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help exits 0 after printing; a bad option must not break the status bar: fallback line, exit 0
        if exc.code not in (0, None):print('TokenAtlas')
        return 0
    if args.setup:
        print(setup_text(db))
        return 0
    model = None
    try:
        payload = json.loads((stdin or sys.stdin).read(), parse_constant=lambda name: None)  # NaN/Infinity are missing values
        model = (payload.get('model') or {}).get('display_name') if isinstance(payload, dict) else None
        cache = read_cache(cache_path(db if db is not None else default_db()))
        now = now or datetime.now(timezone.utc)
        print(render(payload, cache, now))
    except Exception:
        print(model if isinstance(model, str) and model else 'TokenAtlas')
        return 0
    if not args.no_record_quota and not recording_disabled_by_env():
        try:
            sys.stdout.flush()
            record_quota(payload, db if db is not None else default_db(), now)
        except Exception:
            pass  # no snapshot rather than a failed status line
    return 0
