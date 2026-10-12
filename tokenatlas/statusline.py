"""tokenatlas statusline: the Claude Code statusline as a packaged command, plus the small cache `refresh` writes for it.

Claude Code runs the command on every status update, so this module stays light (stdlib and tokenatlas.energy only; the history is never
imported or opened). Day, week and month totals come from statusline.json next to the history database, written by refresh; context and
quota are live from the payload on stdin. No network, no credentials. By default it also appends the 5-hour and weekly quota readings to
claude-quota.jsonl next to the history (see record_quota), so a turn can later be given a share of the limit; `--no-record-quota` or
TOKENATLAS_NO_QUOTA=1 turns that off, and `--record-quota` is accepted as a no-op. That is the only thing it ever writes, and a failure
to write never changes the status line.
"""
import argparse
import base64
import contextlib
import errno
import json
import math
import os
import re
import shlex
import shutil
import signal
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
    invocation = _tokenatlas_invocation(command)
    if invocation is None:return 'unknown'
    if not invocation:return 'foreign'
    # A generated wrapper quotes the entire inner shell command as one argument.
    # Its flags/operators are unrelated to the recorder's options.
    wrapper = None
    try:
        parsed = shlex.split(command)
        if '--wrap' in parsed and '--' in parsed:
            boundary = parsed.index('--')
            outer = parsed[:boundary]
            if '--wrap' in outer:wrapper = outer
    except ValueError:
        return 'unknown'
    prefix = command.split(' -- ', 1)[0] if wrapper is not None else command
    if prefix.lstrip().startswith('& ') and '--wrap-shell-base64' in parsed:
        prefix = prefix.lstrip()[2:]  # PowerShell call operator in our encoded wrapper
    if re.search(r'[;|&`<>\n]|\$\(', prefix):return 'unknown'
    try:tokens = shlex.split(command)
    except ValueError:return 'unknown'
    if wrapper is not None:tokens = wrapper
    local = None
    for token in tokens:
        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*=(.*)$', token, re.S)
        if not match:break
        if token.startswith(NO_QUOTA_ENV + '='):local = match.group(1)
    if '--no-record-quota' in tokens or recording_disabled_by_env(local):return 'disabled'
    return 'enabled'


def _normalise_command_paths(word):
    """Normalize unquoted Windows path words without changing quote boundaries."""
    out, quote, at, beginning, windows = [], None, 0, True, False
    while at < len(word):
        char = word[at]
        if quote:
            out.append(char)
            at += 1
            if quote == '"' and char == '\\' and at < len(word):
                out.append(word[at]); at += 1
            elif char == quote:quote = None
            continue
        if char in ('"', "'"):
            quote = char; beginning = False
            out.append(char); at += 1
            continue
        if beginning:
            windows = bool(re.match('(?:[A-Za-z]:\\\\|\\\\\\\\|\\.{1,2}\\\\|%[A-Za-z_]\\w*%\\\\)', word[at:]))
            beginning = False
        if char == '\\':
            if windows and at + 1 < len(word) and word[at + 1] not in '"\';&|<>() \t\r\n':
                out.append('/'); at += 1
            else:
                out.append(char); at += 1
                if at < len(word):out.append(word[at]); at += 1
        else:
            out.append(char); at += 1
    return ''.join(out)


def _ansi_quoted_word(word):
    """Translate Bash ANSI quote spans to inert POSIX words for classification."""
    out, quote, at = [], None, 0
    escapes = {'a': '\a', 'b': '\b', 'e': '\x1b', 'E': '\x1b', 'f': '\f',
               'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\', "'": "'", '"': '"'}
    def decode(match):
        value = match[1]
        if value in escapes:return escapes[value]
        if value[0] in 'xuU' and len(value) > 1:
            point = int(value[1:], 16)
            return chr(point) if point <= 0x10ffff and not 0xd800 <= point <= 0xdfff else match[0]
        if value[0] in '01234567':return chr(int(value, 8) & 0xff)
        return match[0]
    while at < len(word):
        char = word[at]
        if quote:
            out.append(char); at += 1
            if quote == '"' and char == '\\' and at < len(word):
                out.append(word[at]); at += 1
            elif char == quote:quote = None
            continue
        if word[at:at + 2] == "$'":
            end = at + 2
            while end < len(word):
                if word[end] == '\\':end += 2; continue
                if word[end] == "'":break
                end += 1
            if end >= len(word):raise ValueError('unclosed ANSI quote')
            body = re.sub(r'\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|.)', decode, word[at + 2:end])
            out.append(shlex.quote(body)); at = end + 1; continue
        out.append(char); at += 1
        if char in ('"', "'"):quote = char
        elif char == '\\' and at < len(word):out.append(word[at]); at += 1
    return ''.join(out)


def _command_tokens(command):
    """Shell words plus provenance: quoted operators remain ordinary arguments."""
    tokens, word, quote, ansi, at = [], None, None, False, 0
    def finish(end):
        nonlocal word
        if word is not None:
            values = shlex.split(_normalise_command_paths(_ansi_quoted_word(command[word:end])))
            if len(values) != 1:raise ValueError('ambiguous shell word')
            tokens.append((values[0], False))
            word = None
    while at < len(command):
        char = command[at]
        if quote:
            if (quote == '"' or ansi) and char == '\\':at += 2; continue
            if char == quote:quote, ansi = None, False
            at += 1; continue
        if command[at:at + 2] == "$'":
            if word is None:word = at
            quote, ansi = "'", True
            at += 2; continue
        if char in ('"', "'"):
            if word is None:word = at
            quote = char; at += 1; continue
        if char == '\\':
            if word is None:word = at
            at += 2; continue
        if char.isspace() or char in ';&|<>()':
            finish(at)
            if char == '\n':tokens.append((';', True))
            elif not char.isspace():
                end = at + 1
                while end < len(command) and command[end] in ';&|<>()':end += 1
                tokens.append((command[at:end], True)); at = end; continue
            at += 1; continue
        if word is None:word = at
        at += 1
    if quote:raise ValueError('unclosed shell quote')
    finish(len(command))
    return tokens


def _statusline_operand(args):
    args = list(args)
    while args:
        token = args.pop(0)
        if token == '--db':
            if not args:return False
            args.pop(0)
        elif token.startswith('--db='):continue
        else:return token == 'statusline'
    return False


def _python_statusline(name, args):
    args = list(args)
    flags = {'-I', '-S', '-E', '-s', '-B', '-u', '-O', '-OO', '-q', '-b', '-bb', '-P', '-v', '-x', '-R', '-d', '-i'}
    while args:
        token = args.pop(0)
        if token == '-m':return bool(args and args.pop(0) == 'tokenatlas' and _statusline_operand(args))
        if token.startswith('-m') and len(token) > 2:return token[2:] == 'tokenatlas' and _statusline_operand(args)
        if token in flags:continue
        if name in ('py', 'py.exe') and re.fullmatch(r'-[23](?:\.\d+)?|-V:.+', token):continue
        if token in ('-W', '-X'):
            if not args:return False
            args.pop(0); continue
        if token.startswith(('-W', '-X')) and len(token) > 2:continue
        return False  # -c, a script, stdin or an unknown option ends module selection
    return False


def _shell_source(name, args):
    args = list(args)
    while args:
        token = args.pop(0)
        low = token.lower()
        if name in ('cmd', 'cmd.exe'):
            if low == '/c':return args[0] if len(args) == 1 else ' '.join(shlex.quote(value) for value in args)
            if low in ('/d', '/s', '/q', '/a', '/u') or low.startswith(('/e:', '/v:', '/f:')):continue
        elif name in ('powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
            if low in ('-command', '-c'):return args[0] if len(args) == 1 else ' '.join(args)
            if low in ('-noprofile', '-noninteractive', '-nologo', '-sta', '-mta'):continue
            if low in ('-executionpolicy', '-inputformat', '-outputformat') and args:args.pop(0); continue
        else:
            if re.fullmatch(r'-[eiluxv]*c[eiluxv]*', token):return args[0] if args else None
            if token in ('--noprofile', '--norc') or re.fullmatch(r'-[eiluxv]+', token):continue
        return None  # a script operand or unknown option cannot become a later -c
    return None


def _tokenatlas_invocation(command, depth=0):
    """Recognise supported command positions without interpreting literal data."""
    if not isinstance(command, str) or depth > 3:return False
    try:tokens = _command_tokens(command)
    except ValueError:
        return None if re.search(r'(?i)\b(tokenatlas|energy-monitor)\b', command) and 'statusline' in command else False
    return _invocation_tokens(tokens, depth)

def _launcher_operand(name, args):
    """Return the command operand, never search the launched command's arguments."""
    args = list(args)
    if name == 'uv':
        if args[:2] == ['tool', 'run']:args = args[2:]
        elif args[:1] == ['run']:args = args[1:]
        else:return None
    elif name == 'pipx':
        if args[:1] != ['run']:return None
        args = args[1:]
    values = {
        'uvx': {'--from', '--with', '--with-requirements', '--python', '--index-url', '--directory'},
        'uv': {'--from', '--with', '--with-requirements', '--python', '--directory'},
        'pipx': {'--spec', '--python', '--index-url', '--pip-args'},
        'nice': {'-n', '--adjustment'},
        'timeout': {'-k', '--kill-after', '-s', '--signal'},
        'gtimeout': {'-k', '--kill-after', '-s', '--signal'},
    }[name]
    flags = {'-q', '--quiet', '-v', '--verbose', '--offline', '--no-cache', '--no-config', '--isolated',
             '--foreground', '--preserve-status'}
    while args and args[0].startswith('-'):
        option = args.pop(0)
        if option == '--':break
        key, equals, _value = option.partition('=')
        if key in values:
            if not equals:
                if not args:return None
                args.pop(0)
        elif option in flags or (name == 'nice' and re.fullmatch(r'-[0-9]+|-n-?[0-9]+', option)):
            continue
        else:return None  # unknown option arity cannot prove a command position
    if name in ('timeout', 'gtimeout'):
        if not args:return None
        args.pop(0)  # duration, before the command operand
    if not args:return None
    if name in ('uvx', 'uv', 'pipx') and args[0].startswith('tokenatlas@'):
        args[0] = 'tokenatlas'
    return args


def _invocation_tokens(tokens, depth):
    start, redirected = True, False
    for i, (token, operator) in enumerate(tokens):
        if operator:
            if '<' in token or '>' in token:redirected = True
            else:start = True
            continue
        if redirected:redirected = False; continue
        if not start:continue
        if token.isdigit() and i + 1 < len(tokens) and tokens[i + 1][1] and any(c in tokens[i + 1][0] for c in '<>'):continue
        if re.match(r'[A-Za-z_][A-Za-z0-9_]*=', token) or token in ('exec', 'command', 'env') or token.startswith('-'):continue
        args, at = [], i + 1
        while at < len(tokens):
            value, op = tokens[at]
            if op:
                if '<' in value or '>' in value:at += 2; continue
                break
            args.append(value); at += 1
        name = token.replace('\\', '/').rsplit('/', 1)[-1].lower()
        if name in ('tokenatlas', 'tokenatlas.exe', 'energy-monitor', 'energy-monitor.exe') and _statusline_operand(args):return True
        if (name.startswith('python') or name in ('py', 'py.exe')) and _python_statusline(name, args):return True
        if name in ('uvx', 'uv', 'pipx', 'nice', 'timeout', 'gtimeout'):
            operand = _launcher_operand(name, args)
            if operand:
                found = _tokenatlas_invocation(' '.join(shlex.quote(v) for v in operand), depth + 1)
                if found is not False:return found
        if name in ('sh', 'bash', 'zsh', 'cmd', 'cmd.exe', 'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
            source = _shell_source(name, args)
            if source:
                found = _tokenatlas_invocation(source, depth + 1)
                if found is not False:return found
        start = False
    return False

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


def _launcher_text(command, windows=None):
    """Quote a launcher path, or the interpreter part of a module fallback."""
    suffix = ' -m tokenatlas'
    if command.endswith(suffix):return _quote(command[:-len(suffix)], windows) + suffix
    return _quote(command, windows)


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
    command = _launcher_text(command, windows)
    if db is not None:command += f' --db {_quote(str(Path(db).expanduser().absolute()), windows)}'
    snippet = json.dumps({'statusLine': {'type': 'command', 'command': f"{command} statusline"}}, indent=2)
    warning = ('Warning: a path in this command contains a character that cmd.exe treats specially (&, |, ^, %, ! ...) and that no quoting makes '
               'safe; install TokenAtlas (and the database) under a plain path, or check that the command works before relying on it.\n\n') if unsafe else ''
    return (f'{warning}Add this to {settings} (merge it into the existing JSON; this command never edits the file):\n\n{snippet}\n\n'
            'Totals refresh whenever tokenatlas refresh, open or collect runs; context and quota are live.'
            f' The statusline also records the 5-hour and weekly quota readings Claude Code already shows (timestamp, session id, percentages and reset times) to {QUOTA_NAME} next to the history, '
            'local only (0600), account-wide, only while a Claude Code UI session is open. To turn that off add --no-record-quota to the command or set TOKENATLAS_NO_QUOTA=1.')


def _effective_statusline():
    """Read the same user settings precedence as doctor, without writing it."""
    base = settings_path()
    found = None
    found_path = None
    for path in (base, base.with_name('settings.local.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and 'statusLine' in data:
            found = data['statusLine']
            found_path = path
    if not isinstance(found, dict) or found.get('type', 'command') != 'command' or not isinstance(found.get('command'), str) or not found['command'].strip():
        raise ValueError('no command statusLine found in Claude settings; configure your own command first')
    return dict(found), found_path


def wrap_setup_text(db=None, shell=None):
    original, target = _effective_statusline()
    if _command_state(original['command']) in ('enabled', 'disabled', 'unknown'):
        raise ValueError('the effective statusLine already uses TokenAtlas; keep it or restore your original command first')
    if shell is None and os.name == 'nt':
        raise ValueError('choose --wrap-shell bash or powershell to match the shell Claude Code uses for your statusLine command; cmd is only for an explicitly cmd-based command')
    shell = shell or 'sh'
    if shell == 'sh' and re.search(r'\[\[|\$\x27|(?:^|[;\n])\s*source\s|\b\w+\s*=\s*\(', original['command']):
        raise ValueError('the command contains Bash-specific syntax; use --wrap-shell bash if Bash runs your original statusLine')
    shell_command = _source_command(shell, original['command'])
    command = executable()
    if os.name == 'nt':command = command.replace('\\', '/')
    if os.name == 'nt' and shell == 'cmd':
        launcher = command.removesuffix(' -m tokenatlas')
        paths = [launcher] + ([str(Path(db).expanduser().absolute())] if db is not None else [])
        if any(re.search(r'[&|^%!<>"\r\n]', path) for path in paths):
            raise ValueError('cmd setup requires plain launcher and database paths; cmd metacharacters cannot be quoted safely')
    if shell == 'powershell':
        suffix = ' -m tokenatlas'
        prefix = command[:-len(suffix)] if command.endswith(suffix) else command
        command = "& '" + prefix.replace("'", "''") + "'" + (suffix if command.endswith(suffix) else '')
        quote = lambda value: "'" + value.replace('\\', '/').replace("'", "''") + "'"
    else:
        if os.name == 'nt' and shell == 'cmd':
            # Always quote paths: parentheses and short-path tildes are valid
            # Windows filenames, even when no space makes list2cmdline quote.
            suffix = ' -m tokenatlas'
            command = '"' + command.removesuffix(suffix) + '"' + (suffix if command.endswith(suffix) else '')
            quote = lambda value: '"' + value + '"'
        else:
            command = _launcher_text(command, windows=False)
            quote = shlex.quote
    if db is not None:command += ' --db ' + quote(str(Path(db).expanduser().absolute()))
    if os.name == 'nt':
        # Carry source as inert base64 through the shell Claude uses. Re-run it
        # only in the explicitly selected matching shell inside our job.
        encoded = base64.b64encode(original['command'].encode('utf-8')).decode('ascii')
        suffix = ' statusline --wrap --wrap-shell ' + shell + ' --wrap-shell-base64 ' + encoded
    else:
        suffix = ' statusline --wrap -- ' + ' '.join(shlex.quote(arg) for arg in shell_command)
    wrapped = dict(original, command=command + suffix)
    snippet = json.dumps({'statusLine': wrapped}, indent=2)
    undo = json.dumps({'statusLine': original}, indent=2)
    return (f'Add this to {target} (merge into the effective settings; this command never edits files):\n\n{snippet}\n\n'
            f'Undo: restore the original statusLine in {target}:\n\n{undo}\n\n'
            'Quota is recorded before your command runs. Its stdin, stdout, stderr and exit code are preserved; a timeout returns 124. '
            f'This snippet assumes {shell} runs your original command; choose --wrap-shell to match it. '
            'Managed settings can prevent statusLine changes. Check the effective command with tokenatlas doctor.')


def _wrap_timeout(value):
    try:number = float(value)
    except ValueError:raise argparse.ArgumentTypeError('timeout must be a positive finite number up to 60 seconds') from None
    if not math.isfinite(number) or not 0 < number <= 60:
        raise argparse.ArgumentTypeError('timeout must be a positive finite number up to 60 seconds')
    return number


def _system_command(name):
    """Resolve Windows utilities without consulting cwd, PATH or COMSPEC."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    api.GetSystemDirectoryW.restype = ctypes.c_uint
    count = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):raise ctypes.WinError(ctypes.get_last_error())
    return str(Path(buffer.value) / name)


def _git_bash():
    """Resolve the configured or installed Git Bash, never a cwd/PATH executable."""
    override = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if override:
        path = Path(override)
        if not path.is_absolute() or not path.is_file():
            raise ValueError('CLAUDE_CODE_GIT_BASH_PATH must name the existing absolute Git Bash executable')
        return str(path.resolve())
    import winreg
    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\GitForWindows', 0, winreg.KEY_READ | view) as key:
                    folder, _ = winreg.QueryValueEx(key, 'InstallPath')
                if isinstance(folder, str) and Path(folder).is_absolute():roots.append(Path(folder))
            except OSError:continue
    drive = Path(_system_command('cmd.exe')).anchor
    roots += [Path(drive) / 'Program Files' / 'Git', Path(drive) / 'Program Files (x86)' / 'Git']
    for root in roots:
        for relative in ('bin/bash.exe', 'usr/bin/bash.exe'):
            path = root / relative
            if path.is_file():return str(path.resolve())
    raise ValueError('Git Bash was not found; use the existing CLAUDE_CODE_GIT_BASH_PATH or select the shell that runs your statusLine')


def _source_command(shell, source):
    """Explicit matching shell for the original command, separate from argv wrapping."""
    if shell in ('sh', 'bash'):
        if os.name == 'nt':
            if shell != 'bash':raise ValueError('use bash for Git Bash on Windows')
            executable = _git_bash()
        else:
            executable = '/bin/' + shell
            if not Path(executable).is_file():raise ValueError(f'{executable} is unavailable; run your shell explicitly with --wrap -- COMMAND')
        return [executable, '-c', source]
    if os.name != 'nt':raise ValueError(f'{shell} setup is available only on Windows')
    if shell == 'powershell':
        encoded = base64.b64encode(source.encode('utf-16-le')).decode('ascii')
        return [_system_command('WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    return [_system_command('cmd.exe'), '/d', '/s', '/c', source]


_WRAP_HELPER = '''import json, os, subprocess, sys
header = sys.stdin.buffer.readline()
if not header: raise SystemExit(2)
options = json.loads(header)
argv = options["argv"] if isinstance(options, dict) else options
raw = sys.stdin.buffer.read()
try:
    if os.name == "nt" and isinstance(options, dict) and options.get("shell_source") is not None:
        # cmd.exe parses shell source, not CRT-escaped argv. /s strips these
        # outer quotes, preserving every quote/metacharacter in the source.
        line = subprocess.list2cmdline(argv[:4]) + ' "' + options["shell_source"] + '"'
        result = subprocess.run(line, executable=argv[0], input=raw)
    else:
        result = subprocess.run(argv, input=raw)
except OSError as exc:
    print(f"tokenatlas statusline: cannot run command: {exc}", file=sys.stderr)
    raise SystemExit(127)
raise SystemExit(result.returncode)
'''


def _stop_wrapped(process, job=None):
    """Stop only the isolated child tree created by this wrapper."""
    if os.name == 'nt':
        if job is not None:job.close()  # kill-on-close includes already-orphaned descendants
        if process.poll() is None:process.kill()
    else:
        try:os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:pass
        except PermissionError as exc:
            print(f'tokenatlas statusline: cannot stop wrapped process group: {exc}', file=sys.stderr)


def _wrapper_python():
    """Use the base interpreter, not a Windows venv process redirector."""
    python = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    if sys.prefix != sys.base_prefix and python.is_relative_to(Path(sys.prefix).resolve()):
        raise OSError('cannot identify an external Python for safe Windows statusline supervision')
    return python


def _run_wrapped(args, db, stdin, now):
    stream = stdin if stdin is not None else getattr(sys.stdin, 'buffer', sys.stdin)
    raw = stream.read()
    if isinstance(raw, str):raw = raw.encode('utf-8')
    if not args.no_record_quota and not recording_disabled_by_env():
        try:
            payload = json.loads(raw, parse_constant=lambda name: None)
            if isinstance(payload, dict):record_quota(payload, db if db is not None else default_db(), now or datetime.now(timezone.utc))
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            pass  # malformed input or unavailable recorder must not change the user's output
    job = None
    process = None
    previous = {}
    starting = True
    pending_signal = None
    def cancelled(signum, frame):
        nonlocal pending_signal
        pending_signal = signum
        # Do not interrupt Popen before it returns the child handle. Defer the
        # exit until the isolated process (and Windows job) can be cleaned up.
        if not starting:raise SystemExit(128 + signum)
    try:
        signals = (signal.SIGTERM, signal.SIGINT) if os.name == 'nt' else (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        for signum in signals:
            previous[signum] = signal.signal(signum, cancelled)
        try:
            if os.name == 'nt':
                from tokenatlas.upgrade_process import _Job
                python = _wrapper_python()
                job = _Job()
                # The helper cannot launch the command until the parent assigns
                # its job and sends the stdin header.
                process = subprocess.Popen([str(python), '-I', '-S', '-c', _WRAP_HELPER], stdin=subprocess.PIPE,
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                job.assign(process)
                options = {'argv': args.command}
                if args.wrap_shell_base64 is not None and args.wrap_shell == 'cmd':options['shell_source'] = args.command[-1]
                raw = json.dumps(options).encode('utf-8') + b'\n' + raw
            else:
                process = subprocess.Popen(args.command, stdin=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            print(f'tokenatlas statusline: cannot run command: {exc}', file=sys.stderr)
            return 127
        starting = False
        if pending_signal is not None:raise SystemExit(128 + pending_signal)
        process.communicate(raw, timeout=args.wrap_timeout)
    except subprocess.TimeoutExpired:
        _stop_wrapped(process, job)
        try:process.communicate(timeout=1)
        except subprocess.TimeoutExpired:pass  # cleanup remains bounded even for an uninterruptible child
        print('tokenatlas statusline: wrapped command timeout', file=sys.stderr)
        return 124
    finally:
        # A successful direct child may leave descendants holding the output
        # pipes. A statusline has no persistent background process contract.
        starting = True  # finish cleanup even if another termination arrives
        try:
            if process is not None:
                _stop_wrapped(process, job)
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    print('tokenatlas statusline: wrapped process cleanup deadline exceeded', file=sys.stderr)
            elif job is not None:job.close()
        finally:
            for signum, handler in previous.items():signal.signal(signum, handler)
        if pending_signal is not None:raise SystemExit(128 + pending_signal)
    return process.returncode if process.returncode >= 0 else 128 - process.returncode


def run(argv, db=None, stdin=None, now=None):
    """Entry point: print one line and return 0, whatever happens. It records quota readings unless --no-record-quota or TOKENATLAS_NO_QUOTA=1 (record_quota); --record-quota is a no-op."""
    parser = argparse.ArgumentParser(prog='tokenatlas statusline', description='Claude Code statusline: reads its JSON payload on stdin and the cache refresh writes; no network.')
    parser.add_argument('--setup', action='store_true', help="Print the statusLine snippet for Claude Code's settings and its location, without editing it.")
    parser.add_argument('--record-quota', action='store_true', help='Accepted for compatibility; recording is on by default.')
    parser.add_argument('--no-record-quota', action='store_true', help=f'Do not append the 5-hour and weekly quota readings to {QUOTA_NAME} next to the history (also TOKENATLAS_NO_QUOTA=1).')
    parser.add_argument('--wrap', action='store_true', help='Record quota first, then run the command after -- with unchanged stdin and output.')
    parser.add_argument('--wrap-timeout', type=_wrap_timeout, default=5, metavar='SECONDS', help='Wrapped command deadline (default 5 seconds; at most 60).')
    parser.add_argument('--wrap-shell', choices=('sh', 'bash', 'powershell', 'cmd'), help='Shell Claude uses for the original command. With --setup --wrap: sh by default on POSIX; required on Windows (Git Bash or PowerShell).')
    parser.add_argument('--wrap-shell-base64', help=argparse.SUPPRESS)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help exits 0 after printing; a bad option must not break the status bar: fallback line, exit 0
        if '--wrap' in argv:return int(exc.code or 0)
        if exc.code not in (0, None):print('TokenAtlas')
        return 0
    if args.command[:1] == ['--']:args.command = args.command[1:]
    if args.wrap_shell_base64 is not None:
        if not args.wrap or args.command or args.setup:
            print('tokenatlas statusline: encoded shell source requires --wrap alone', file=sys.stderr)
            return 2
        try:
            source = base64.b64decode(args.wrap_shell_base64, validate=True).decode('utf-8')
            if not source.strip() or '\x00' in source:raise ValueError('invalid shell source')
        except (ValueError, UnicodeError) as exc:
            print(f'tokenatlas statusline: invalid encoded command: {exc}', file=sys.stderr)
            return 2
        if args.wrap_shell is None:
            print('tokenatlas statusline: encoded command requires --wrap-shell', file=sys.stderr)
            return 2
        try:args.command = _source_command(args.wrap_shell, source)
        except (OSError, ValueError) as exc:
            print(f'tokenatlas statusline: cannot resolve shell: {exc}', file=sys.stderr)
            return 127
    if args.command and not args.wrap:
        print('TokenAtlas')
        return 0
    if args.wrap_shell is not None and not (args.setup and args.wrap) and args.wrap_shell_base64 is None:
        print('tokenatlas statusline: --wrap-shell requires --setup --wrap or encoded shell source', file=sys.stderr)
        return 2
    if args.setup:
        try:print(wrap_setup_text(db, args.wrap_shell) if args.wrap else setup_text(db))
        except ValueError as exc:
            print(f'tokenatlas statusline: {exc}', file=sys.stderr)
            return 2
        return 0
    if args.wrap:
        if not args.command:
            print('tokenatlas statusline: --wrap requires a command after --', file=sys.stderr)
            return 2
        return _run_wrapped(args, db, stdin, now)
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
