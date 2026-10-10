"""Advisory per-environment lock shared by collect and the explicit updater."""
import contextlib
import contextvars
import os
import stat
import sys
from pathlib import Path

_held_fd = contextvars.ContextVar('tokenatlas_install_lock',default=None)


def inherited_fds():
    fd = _held_fd.get()
    return (fd,) if fd is not None and os.name != 'nt' else ()


class Busy(ValueError):
    pass


@contextlib.contextmanager
def installation_lock(prefix=None):
    prefix = Path(prefix or sys.prefix).resolve()
    # System/source executions have no mutable tool environment to coordinate.
    if prefix == Path(sys.base_prefix).resolve():
        yield
        return
    path = prefix.parent / ('.' + prefix.name + '.tokenatlas.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or path.is_symlink():
            raise ValueError('installation lock must be a regular file')
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Busy('collection or upgrade is already running; retry after it finishes') from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Busy('collection or upgrade is already running; retry after it finishes') from exc
        token = _held_fd.set(fd)
        try:yield
        finally:_held_fd.reset(token)
    finally:
        os.close(fd)
