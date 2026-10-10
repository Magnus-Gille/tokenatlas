"""Explicit, conservative package-manager upgrade. Never opens usage history."""
import importlib.metadata as metadata
import importlib.util
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tokenatlas import __version__
from tokenatlas.install_lock import installation_lock, inherited_fds, Busy
from tokenatlas.terminal import terminal_safe
from tokenatlas.upgrade_index import latest_version, validate_version


class Unsupported(ValueError):
    pass


class Failed(ValueError):
    pass


@dataclass(frozen=True)
class Installation:
    manager: str
    prefix: Path
    python: Path
    launcher: Path
    manager_exe: Path | None
    exposed: Path | None = None


def _inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _execute(argv, *, timeout=600):
    # Never inherit a source checkout's PYTHONPATH into installation/health checks.
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(key, None)
    for key in list(env):
        if (key.startswith('PIP_') or key == 'PYTHONUSERBASE' or
            (key.startswith('UV_') and key not in ('UV_TOOL_DIR','UV_TOOL_BIN_DIR','UV_CACHE_DIR',
                                                   'UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_OFFLINE'))):
            env.pop(key)
    env['PIP_CONFIG_FILE'] = os.devnull
    env['PIP_NO_INPUT'] = '1'
    env['UV_NO_CONFIG'] = '1'
    if os.name == 'nt':
        from tokenatlas.upgrade_process import execute_windows
        return execute_windows(argv,env,tempfile.gettempdir(),timeout)
    options = {'start_new_session': True,'pass_fds':inherited_fds()}
    proc = subprocess.Popen([str(x) for x in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace',
                            env=env, cwd=tempfile.gettempdir(), **options)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    handlers = {}
    for name in ('SIGTERM','SIGHUP'):
        if hasattr(signal,name):
            sig = getattr(signal,name)
            handlers[sig] = signal.signal(sig,interrupted)
    try:
        out, err = proc.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired,KeyboardInterrupt):
        # Keep the installation lock until the installer tree is stopped.
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.communicate()
        raise
    finally:
        for sig, handler in handlers.items():signal.signal(sig,handler)
    return subprocess.CompletedProcess(argv,proc.returncode,out,err)


def _manager_path(name, prefix):
    executable = shutil.which(name)
    if not executable or _inside(executable, prefix):
        raise Unsupported(f'{name} is missing or lives inside this environment; use the owning {name} installation to update it')
    return Path(executable).absolute()


def _query(argv):
    result = _execute(argv, timeout=15)
    if result.returncode:
        raise Unsupported('package manager could not confirm ownership; inspect its tool/environment listing')
    return result.stdout.strip()


def detect():
    prefix = Path(sys.prefix).resolve()
    python = Path(sys.executable).absolute()  # Do not resolve a venv's Python symlink to system Python.
    module = Path(__file__).resolve()
    if prefix == Path(sys.base_prefix).resolve() or not (prefix/'pyvenv.cfg').is_file():
        raise Unsupported('system or externally managed Python: install TokenAtlas with pipx, uv tool, or a dedicated venv first')
    cfg = (prefix/'pyvenv.cfg').read_text().lower().replace(' ','')
    if 'include-system-site-packages=true' in cfg:
        raise Unsupported('venv exposes system packages; use a dedicated isolated tool environment')
    if not _inside(module, prefix):
        raise Unsupported('source checkout shadows the installed package; run the installed CLI outside the checkout without PYTHONPATH')
    try:
        distribution = metadata.distribution('tokenatlas')
        direct = json.loads(distribution.read_text('direct_url.json') or '{}')
    except (metadata.PackageNotFoundError, ValueError) as exc:
        raise Unsupported('cannot identify the installed TokenAtlas distribution') from exc
    if not _inside(distribution.locate_file('tokenatlas'), prefix):
        raise Unsupported('TokenAtlas is loaded from another environment; use its owning package manager')
    if direct and (not isinstance(direct, dict) or 'dir_info' in direct or 'vcs_info' in direct or not str(direct.get('url','')).split('?',1)[0].endswith('.whl')):
        raise Unsupported('editable/source installation: update the source with its original installation method')
    if distribution.version != __version__:
        raise Unsupported('package metadata and imported version disagree; repair using the original package manager')
    scripts = prefix / ('Scripts' if os.name == 'nt' else 'bin')
    launcher = scripts / ('tokenatlas.exe' if os.name == 'nt' else 'tokenatlas')
    if not launcher.is_file() or not _inside(launcher, prefix):
        raise Unsupported('the active environment has no unambiguous TokenAtlas launcher')
    # A global external-management marker does not disqualify an actual venv;
    # an explicit marker inside this environment does.
    libraries = [Path(sysconfig.get_path(name)) for name in ('stdlib','platstdlib')]
    if ((prefix/'EXTERNALLY-MANAGED').exists() or any(
        _inside(path,prefix) and (path/'EXTERNALLY-MANAGED').exists() for path in libraries
    )):
        raise Unsupported('this environment is externally managed; use its owner to update it')
    pipx = (prefix/'pipx_metadata.json').is_file()
    uv = (prefix/'uv-receipt.toml').is_file()
    if pipx and uv:
        raise Unsupported('ambiguous pipx and uv ownership; inspect the original installation')
    allowed = {'tokenatlas','tzdata','pip','setuptools','wheel','packaging'}
    installed = {str(d.metadata['Name']).lower().replace('_','-') for d in metadata.distributions()}
    if installed - allowed:
        raise Unsupported('environment contains other applications; update TokenAtlas explicitly using its owning manager')
    manager, executable = 'venv', None
    if pipx:
        executable = _manager_path('pipx',prefix)
        home = Path(_query([executable,'environment','--value','PIPX_HOME'])).resolve()
        try:
            record = json.loads((prefix/'pipx_metadata.json').read_text())['main_package']
            owned = record['package'] == 'tokenatlas' and not record.get('suffix') and 'tokenatlas' in record['apps']
        except (ValueError, KeyError, TypeError):
            owned = False
        if not owned or prefix != home/'venvs'/'tokenatlas':
            raise Unsupported('pipx ownership/suffix is not supported; use pipx for this named environment')
        manager = 'pipx'
    elif uv:
        executable = _manager_path('uv',prefix)
        home = Path(_query([executable,'tool','dir'])).resolve()
        if prefix != home/'tokenatlas':
            raise Unsupported('uv tool directory does not own the active environment; use its original UV_TOOL_DIR')
        manager = 'uv'
    else:
        # Unknown manager environments must not silently become pip environments.
        if prefix.parent.name == 'venvs' or prefix.parent.name in ('tools','pipx') or (prefix/'conda-meta').exists():
            raise Unsupported('unrecognized manager environment; use its original installer')
        if importlib.util.find_spec('pip') is None:
            raise Unsupported('dedicated venv has no pip; use its original installer or recreate it with pip')
    exposed = None
    if manager != 'venv':
        args = [executable,'tool','dir','--bin'] if manager=='uv' else [executable,'environment','--value','PIPX_BIN_DIR']
        bindir = Path(_query(args))
        exposed = bindir / launcher.name
        if not exposed.is_file() or not (os.path.samefile(exposed, launcher) or
                (os.name == 'nt' and exposed.read_bytes() == launcher.read_bytes())):
            raise Unsupported('manager-exposed TokenAtlas command does not match this environment; repair the manager entrypoint first')
    return Installation(manager,prefix,python,launcher,executable,exposed)


def command(installation, target):
    validate_version(target)
    spec = 'tokenatlas==' + target
    if installation.manager == 'venv':
        return [str(installation.python),'-I','-m','pip','--isolated','install','--upgrade','--no-user',
                '--prefix',str(installation.prefix),'--index-url','https://pypi.org/simple','--only-binary=:all:',spec]
    if installation.manager == 'pipx':
        return [str(installation.manager_exe),'install','--force','--upgrade','--index-url','https://pypi.org/simple',spec]
    if installation.manager == 'uv':
        base = Path(getattr(sys,'_base_executable',sys.executable)).resolve()
        if _inside(base,installation.prefix):
            raise Unsupported('cannot identify a Python outside the uv environment; update with uv tool directly')
        return [str(installation.manager_exe),'--no-config','tool','install','--python',str(base),
                '--no-python-downloads','--index-url','https://pypi.org/simple',spec]
    raise Unsupported('unknown installation manager')


def _embedded_state(prefix):
    names = {'history.sqlite3','report.html','remote-hosts','quota-budget.json','usage-profile.json',
             'top-prompts.json','outcomes.jsonl','statusline.json','claude-quota.jsonl',
             'claude-quota.last','claude-quota.lock','collect.lock','collect.log',
             'com.tokenatlas.collect.plist','tokenatlas-collect.service','tokenatlas-collect.timer'}
    for path in prefix.rglob('*'):
        if path.name in names or path.name.endswith(('-wal','-shm','-journal')):
            return True
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as file:header=file.read(16)
            if header.startswith((b'SQLite format 3\0',b'\x37\x7f\x06\x82',b'\x37\x7f\x06\x83',b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7')):
                return True
    return False


def snapshot(installation):
    """Copy code/environment only, alongside it; never roll usage data backward."""
    # Tool managers may replace an entire environment. Usage/state does not
    # belong there, and must not be silently swept into a code-only restore.
    state_root = os.environ.get('XDG_STATE_HOME')
    state = (Path(state_root) if state_root else Path.home()/'.local/state')/'tokenatlas'
    if _inside(state,installation.prefix) or _embedded_state(installation.prefix):
        raise Unsupported('usage data/configuration is inside the tool environment; move it outside before upgrading')
    backup = Path(tempfile.mkdtemp(prefix='.tokenatlas-backup-',dir=installation.prefix.parent))
    try:
        shutil.copytree(installation.prefix,backup/'environment',symlinks=True)
        # Copy success alone is not a recovery receipt. Verify every saved regular
        # file and symlink before allowing the package manager to run.
        manifest = {}
        for source in installation.prefix.rglob('*'):
            relative = source.relative_to(installation.prefix)
            saved = backup/'environment'/relative
            if source.is_symlink():
                if not saved.is_symlink() or os.readlink(source) != os.readlink(saved):
                    raise OSError('backup symlink mismatch')
            elif source.is_file():
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if hashlib.sha256(saved.read_bytes()).hexdigest() != digest:
                    raise OSError('backup file mismatch')
                manifest[str(relative)] = digest
        record = {'version':__version__,'manager':installation.manager,'environment':str(installation.prefix),
                  'python':str(installation.python),'launcher':str(installation.launcher),'sha256':manifest}
        if installation.exposed:
            shutil.copy2(installation.exposed,backup/'exposed-launcher',follow_symlinks=False)
            record['exposed_launcher'] = str(installation.exposed)
            saved = backup/'exposed-launcher'
            if installation.exposed.is_symlink():
                record['exposed_symlink'] = os.readlink(installation.exposed)
                if not saved.is_symlink() or os.readlink(saved) != record['exposed_symlink']:
                    raise OSError('backup exposed symlink mismatch')
            else:
                digest = hashlib.sha256(installation.exposed.read_bytes()).hexdigest()
                if hashlib.sha256(saved.read_bytes()).hexdigest() != digest:
                    raise OSError('backup exposed executable mismatch')
                record['exposed_sha256'] = digest
        (backup/'recovery.json').write_text(json.dumps(record,indent=2)+'\n')
        (backup/'README.txt').write_text(
            'Previous TokenAtlas environment; keep until the new version is accepted.\n'
            'Stop TokenAtlas processes before recovery. Using a Python outside this environment,\n'
            'rename the entire damaged environment to a new unused sibling path first.\n'
            'Then copy the entire saved environment/ directory to the now-absent exact\n'
            'environment path in recovery.json, preserving symlinks. NEVER overlay directories: newer files must not remain.\n'
            'Its scripts refer to that original path; do not run them from this backup directory.\n'
            'If present, restore exposed-launcher to exposed_launcher from recovery.json, preserving symlinks.\n'
            'Verify: run the recorded python with -I -c "import importlib.metadata as m, tokenatlas; print(tokenatlas.__version__, m.version(\'tokenatlas\'))".\n'
            'Both values must equal the saved version. Run the recorded launcher and exposed_launcher with --version too.\n'
            'This is a code backup, not permission to restore usage history or newer data.\n')
    except (OSError,ValueError) as exc:
        # Partial backup is not advertised as recoverable; installation has not started.
        raise Failed(f'could not complete environment backup at {backup}; installation was not changed') from exc
    return backup


def health(installation, target):
    """Isolated import/resources and real launcher, without doctor/history side effects."""
    probe = ('import importlib.metadata as m, pathlib, tokenatlas; '
             'from tokenatlas import history, report; '
             'p=pathlib.Path(tokenatlas.__file__).resolve(); '
             'assert tokenatlas.__version__ == m.version("tokenatlas") == ' + repr(target) + '; '
             'assert p.is_relative_to(pathlib.Path(' + repr(str(installation.prefix)) + ')); '
             'assert (p.parent/"report_template.html").is_file()')
    checked = _execute([installation.python,'-I','-c',probe],timeout=30)
    if checked.returncode:
        raise Failed('installed package failed isolated version/resource health checks')
    checked = _execute([installation.launcher,'--version'],timeout=30)
    if checked.returncode or checked.stdout.strip() != 'tokenatlas '+target:
        raise Failed('installed executable did not report the requested version')
    if installation.exposed:
        checked = _execute([installation.exposed,'--version'],timeout=30)
        if checked.returncode or checked.stdout.strip() != 'tokenatlas '+target:
            raise Failed('manager-exposed executable did not report the requested version')


def mutate(installation, target):
    backup = snapshot(installation)
    print(terminal_safe(f'Previous environment backup: {backup}'),flush=True)
    try:
        result = _execute(command(installation,target))
        if result.returncode:
            raise Failed(f'package manager exited {result.returncode}; inspect the owning manager installation')
        health(installation,target)
    except (OSError, subprocess.SubprocessError, Failed, KeyboardInterrupt) as exc:
        # Manager output may include private index credentials. Never echo it.
        detail = str(exc) if isinstance(exc, Failed) else type(exc).__name__
        raise Failed(f'{detail}; update/verification failed; prior environment backup: {backup}. '
                     f'See README.txt there for recovery. Usage history was not restored.') from exc
    print(f'Installed and verified TokenAtlas {target}.')
    print('Rebuild existing dashboard HTML with: tokenatlas open --no-refresh')


def run(args):
    try:
        target = validate_version(args.version) if args.version else None
    except ValueError as exc:
        print(terminal_safe(exc),file=sys.stderr)
        return 2
    try:
        try:
            installation = detect()
            details = f'{installation.manager}; environment {installation.prefix}; Python {installation.python}'
        except Unsupported as exc:
            if not args.check:
                raise
            installation = None
            details = f'unsupported: {exc}'
        if target is None:
            try:
                target = latest_version()
            except ValueError as exc:
                raise Failed(str(exc)) from exc
        print(terminal_safe(f'Current: {__version__}; target: {target}; {details}'))
        if args.check or target == __version__:
            return 0
        if args.version is None and tuple(map(int,target.split('.'))) < tuple(map(int,__version__.split('.'))):
            print('Already newer than the latest stable release; use --version for an intentional rollback.')
            return 0
        if os.name == 'nt' and Path(sys.argv[0]).suffix.lower() == '.exe':
            raise Unsupported(f'Windows cannot replace the running console launcher. Run: "{installation.python}" -m tokenatlas upgrade --version {target} --yes')
        if not args.yes:
            if not sys.stdin.isatty():
                raise Unsupported('noninteractive upgrade requires --yes; use --check to inspect without changing anything')
            if input('Back up this environment and install this version? [y/N] ').strip().lower() not in ('y','yes'):
                raise Unsupported('upgrade cancelled; installation was not changed')
        print(terminal_safe('Command: '+shlex.join(command(installation,target))),flush=True)
        with installation_lock(installation.prefix):
            if detect() != installation:
                raise Unsupported('installation changed while waiting; rerun upgrade from the current CLI')
            mutate(installation,target)
        return 0
    except Busy as exc:
        print(terminal_safe(exc),file=sys.stderr)
        return 3
    except Unsupported as exc:
        print(terminal_safe(exc),file=sys.stderr)
        return 2
    except (Failed,OSError,subprocess.SubprocessError,EOFError) as exc:
        print(terminal_safe(exc),file=sys.stderr)
        return 1
