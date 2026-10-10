"""tokenatlas collect: one scheduled run (refresh, top text, report, remote sync, report) under a kernel lock."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from tokenatlas import progress
from tokenatlas.terminal import terminal_safe

PACKAGED_SYNC=Path(__file__).with_name('remote_sync.sh')
GRACE=5  # seconds between TERM and KILL of the sync group; longer than remote_sync.sh's own 2 s
POLL=0.2  # seconds between checks for a noted signal while the sync runs


def log(msg):
    with progress.suspend():print(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} collect: {msg}",flush=True)  # stdout stays as the cron logs have it; a live line steps aside


def _lock(fd):
    """Take the exclusive non-blocking lock; False when another process holds it. The kernel drops it on exit or crash."""
    if os.name=='nt':
        import msvcrt
        os.lseek(fd,0,os.SEEK_SET)
        try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        except OSError:return False
        return True
    import fcntl
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:return False
    return True


def _step(name,fn):
    """Run fn() -> exit code (an exception is a failure); log one line with exit status and timing; return the code."""
    start=time.monotonic()
    with progress.step(name[:1].upper()+name[1:]) as shown:  # a terminal shows the step live; the log line below is unchanged
        try:rc=fn()
        except SystemExit as exc:rc=exc.code if isinstance(exc.code,int) else 1
        except Exception as exc:
            log(f'{name}: {type(exc).__name__}: {terminal_safe(exc)}');rc=1
        shown.failed=bool(rc)
    log(f'{name} exit={rc} ({time.monotonic()-start:.1f}s)')
    return rc


def _quiet(call,*argv):
    """Run a CLI command in-process with its stdout/stderr kept off the log, except stderr lines on failure."""
    out,err=io.StringIO(),io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
        try:rc=call(list(argv))
        except SystemExit as exc:rc=exc.code if isinstance(exc.code,int) else 1
    if rc:
        for line in err.getvalue().splitlines()[-5:]:log(f'  {terminal_safe(line)}')
    return rc


def _kept_choice(db):
    """['-n',k,'--by',by] from the store's recorded choice (read with its safety checks), so collect keeps it and never raises k; None when there is no valid pair."""
    from tokenatlas import prompt_store
    k,by=prompt_store.load_meta(prompt_store.store_path(db))[1:]
    return ['-n',str(k),'--by',by] if prompt_store.valid_choice(k,by) else None


def _top_up_to_date(db, revision, history_token=None, machine=None, prices=None):
    from tokenatlas import prompt_store
    return prompt_store.up_to_date(prompt_store.store_path(db), revision, history_token, machine, prices)


def _hosts(args,state):
    if args.remote:return ' '.join(args.remote)
    if os.environ.get('REMOTE_HOSTS_OVERRIDE','').strip():return os.environ['REMOTE_HOSTS_OVERRIDE'].strip()
    try:return ' '.join((state/'remote-hosts').read_text().split())
    except OSError:return ''


class Terminated(BaseException):
    """Raised by the signal handler so the sync group is cleaned up before collect exits 128+signal."""
    def __init__(self,sig):super().__init__(sig);self.sig=sig


_DEFER={'depth':0,'pending':None}


def _on_signal(sig,_frame):
    if _DEFER['depth']:_DEFER['pending']=_DEFER['pending'] or sig;return
    raise Terminated(sig)


@contextlib.contextmanager
def _deferred():
    """Hold TERM/INT/HUP while a step must not stop half-way (starting the sync and taking charge of it, stopping its group); a signal that
    arrived meanwhile is raised as soon as the step is done."""
    _DEFER['depth']+=1
    try:yield
    finally:
        _DEFER['depth']-=1
        if not _DEFER['depth'] and _DEFER['pending']:
            sig,_DEFER['pending']=_DEFER['pending'],None
            raise Terminated(sig)


def _stop_group(proc,grace=GRACE):
    """TERM the sync's process group, wait up to grace s for every member to go (the leader is reaped as it exits), then KILL the group."""
    with contextlib.suppress(ProcessLookupError,PermissionError):os.killpg(proc.pid,signal.SIGTERM)
    deadline=time.monotonic()+grace
    while time.monotonic()<deadline:
        proc.poll()
        try:os.killpg(proc.pid,0)
        except (ProcessLookupError,PermissionError):break
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError,PermissionError):os.killpg(proc.pid,signal.SIGKILL)
    proc.wait()


def _sync(script,hosts,timeout,db,lock_fd):
    """Run the remote sync script in its own session, holding the collect lock (the fd is inherited, so the lock lives until the whole
    tree is gone even if collect dies). On timeout or a signal: TERM the group, wait longer than the script's own 2 s grace, KILL it."""
    if not script.is_file():log(f'{script} not found');return 127
    # One group: remote_sync.sh must not use job control, so every descendant stays in the group that is signalled.
    env={**os.environ,'REMOTE_HOSTS_OVERRIDE':hosts,'TOKENATLAS_DB':str(db),'TOKENATLAS_SINGLE_GROUP':'1'}
    # A signal never interrupts this function: it is noted, the group is stopped, and it is raised on the way out (no bytecode gap in which
    # the sync could be left running without its supervisor).
    with _deferred(),progress.suspend():  # the script prints to the inherited stdout/stderr: no live line may interleave with it
        from tokenatlas.install_lock import inherited_fds
        proc=subprocess.Popen(['bash',str(script)],env=env,start_new_session=True,pass_fds=(lock_fd,*inherited_fds()))
        deadline=time.monotonic()+timeout
        while True:
            if _DEFER['pending']:_stop_group(proc);return None
            left=deadline-time.monotonic()
            if left<=0:log(f'remote sync: timeout after {timeout}s');_stop_group(proc);return 124
            try:return proc.wait(timeout=min(POLL,left))
            except subprocess.TimeoutExpired:pass


def run(args):
    from tokenatlas.install_lock import installation_lock, Busy
    try:
        with installation_lock():return _run(args)
    except Busy:log('collection or upgrade already running; skipped');return 0
    except OSError as exc:log(f'cannot open installation lock: {terminal_safe(exc)}');return 1
    except Terminated as exc:log(f'terminated by signal {exc.sig}');return 128+exc.sig


def _run(args):
    from tokenatlas import prompt_store
    from tokenatlas.__main__ import main,refresh_all
    from tokenatlas.history import History
    db=args.db.expanduser();state=db.parent
    try:state.mkdir(parents=True,exist_ok=True);lock=open(state/'collect.lock','a+')
    except OSError as exc:log(f'cannot open lock in {state}: {exc}');return 1
    with lock:
        if not _lock(lock.fileno()):log('already running');return 0
        for name in ('SIGTERM','SIGINT','SIGHUP'):
            if hasattr(signal,name):signal.signal(getattr(signal,name),_on_signal)
        def refresh():
            with History(db) as history:result=refresh_all(history)
            return 0 if result['status']=='ok' else 2
        def report():  # at most one build an hour while only the data changes (an option or privacy change always rebuilds)
            return lambda:_quiet(main,'--db',str(db),'report','--html',str(state/'report.html'),'--private','--if-changed','--max-age','1h','--lang',args.lang)
        failed=_step('refresh',refresh)!=0
        if prompt_store.store_path(db).exists():
            choice=_kept_choice(db)
            if choice is None:log(f'top: skipped: {prompt_store.store_path(db)} has no valid recorded k/by; choose one with tokenatlas top --keep-text -n N (or --forget-text)');failed=True
            else:
                with History(db) as history:
                    current_revision, history_token, machine = history.revision, history.revision_token, history.machine
                from tokenatlas import budget, pricing
                prices = budget.table_id(pricing.load_prices())
                if _top_up_to_date(db, current_revision, history_token, machine, prices):
                    log('top: skipped: history revision and stored choice are unchanged')
                else:
                    failed|=_step('top',lambda:_quiet(main,'--db',str(db),'top','--keep-text',*choice))!=0
        if not args.no_report:failed|=_step('report',report())!=0
        hosts=_hosts(args,state)
        if hosts:
            script=Path(args.remote_sync or os.environ.get('TOKENATLAS_REMOTE_SYNC') or PACKAGED_SYNC)
            if os.name=='nt':log('remote sync skipped: bash is not assumed on Windows')
            else:
                failed|=_step('remote sync',lambda:_sync(script,hosts,args.sync_timeout,db,lock.fileno()))!=0
                if not args.no_report:failed|=_step('report after sync',report())!=0  # partial imports show up even after a failed sync
        return int(failed)
