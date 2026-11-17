"""Work shared between the steps of one process (TOKEN-189).

`collect` runs top and report one after the other in the same process; both read the whole history, resolve projects,
assign prompts, find limit hits and price every request. Inside `scope()` such results are computed once and reused while
the history is unchanged: the loaded history is keyed by database, revision, revision token and project roots, and every
other result by the identity of the objects it was computed from (which are kept alive, so an id is never reused).
Outside a scope every call computes as before.
"""
import os
from contextlib import contextmanager
from pathlib import PurePath

_state = None


@contextmanager
def scope():
    """Share results until the outermost scope ends; nested scopes join the outer one."""
    global _state
    if _state is not None:
        yield
        return
    _state = {}
    try:
        yield
    finally:
        _state = None


def active():
    return _state is not None


def _key(value):
    if isinstance(value, (list, tuple)) and not value:
        return ('empty',)
    if isinstance(value, PurePath):  # a file read by the call: its path and its current size and time, so a change is seen
        try:
            st = os.stat(value)
        except OSError:
            return ('path', str(value), None)
        return ('path', str(value), st.st_size, st.st_mtime_ns)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return tuple(_key(v) for v in value)
    if isinstance(value, frozenset):
        return frozenset(_key(v) for v in value)
    return ('id', id(value))


def call(fn, *args, **kwargs):
    """fn(*args, **kwargs), computed once per scope for the same arguments (objects by identity, plain values by value).
    The cached result is returned as is: callers must treat it as read-only or copy it."""
    if _state is None:
        return fn(*args, **kwargs)
    key = ('call', fn.__module__, fn.__qualname__, _key(args), _key(tuple(sorted(kwargs.items()))))
    hit = _state.get(key)
    if hit is None:
        hit = _state[key] = (args, kwargs, fn(*args, **kwargs))
    return hit[2]


def history(key, load):
    """The loaded history for `key` (database, revision, ...): load() once while the key holds. A new key drops everything
    shared so far, because every other result was derived from the previous history."""
    if _state is None:
        return load()
    if _state.get('history_key') != key:
        if 'history_key' in _state:  # results computed before the first load (a price table) stay
            _state.clear()
        _state['history_key'] = key
        _state['history'] = load()
    return _state['history']


def memo(name):
    """A dict that lives for the scope (None outside one), for a module's own per-object memo such as prices."""
    if _state is None:
        return None
    return _state.setdefault(('memo', name), {})
