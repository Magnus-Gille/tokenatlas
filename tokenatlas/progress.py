"""Progress on stderr for slow commands: one live line (spinner, step, count, elapsed) on a terminal, plain lines with TOKENATLAS_PROGRESS=1,
nothing when stderr is not a terminal or TOKENATLAS_PROGRESS=0. Never writes to stdout. Deep code reports through the module-level
current() instance (a no-op until `start()`), so no parameter is threaded through the call chain."""
import contextlib
import os
import shutil
import sys
import threading
import time

SPIN,TICK,CROSS=('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏','✓','✗')
ASCII=('|/-\\','ok','x')
INTERVAL=0.1  # seconds between redraws of the live line


def _encodes(stream,text):
    try:text.encode(getattr(stream,'encoding',None) or 'ascii')
    except (UnicodeEncodeError,LookupError):return False
    return True


class Step:
    """Handle of a running step; set .failed (collect's exit codes) to end it as failed without an exception."""
    def __init__(self,label,start,depth):self.label,self.start,self.depth,self.failed=label,start,depth,False


class Progress:
    def __init__(self,stream,mode,ascii=False,clock=time.monotonic,interval=INTERVAL,width=None):
        """mode: 'live' (animated line, finished steps kept), 'plain' (a line at step start and end) or 'off'."""
        self.stream,self.mode,self.clock,self.interval=stream,mode,clock,interval
        self.spin,self.tick,self.cross=ASCII if ascii else (SPIN,TICK,CROSS)
        self.width=width  # fixed terminal width (tests); else asked of the terminal on each draw
        self.lock=threading.RLock()
        self.steps,self.count_text,self.note_text,self.frame,self.drawn,self.paused=[],'','',0,0,0
        self.thread,self.stop_event=None,threading.Event()

    @classmethod
    def for_stream(cls,stream,env=None,**kw):
        """Mode from TOKENATLAS_PROGRESS (1 plain, 0 off) else live on a terminal and off elsewhere; ASCII when the stream cannot encode the glyphs."""
        value=(os.environ if env is None else env).get('TOKENATLAS_PROGRESS','').strip()
        try:tty=bool(stream.isatty())
        except (AttributeError,ValueError,OSError):tty=False
        mode='off' if value=='0' else 'plain' if value=='1' else 'live' if tty else 'off'
        return cls(stream,mode,ascii=not _encodes(stream,SPIN+TICK+CROSS),**kw)

    # drawing (always under the lock)
    def _write(self,text):
        try:self.stream.write(text);self.stream.flush()
        except (OSError,ValueError):self.mode='off'  # a closed or broken stderr must never break the command

    def _clear(self):
        if self.drawn:self._write('\r'+' '*self.drawn+'\r');self.drawn=0

    def _draw(self):
        if self.mode!='live' or not self.steps or self.paused:return
        step=self.steps[-1]
        outer=self.steps[0].label if len(self.steps)>1 else ''
        parts=[f'{outer}: ' if outer else '',step.label,f' {self.note_text}' if self.note_text else '',f' {self.count_text}' if self.count_text else '',f' {self.clock()-step.start:.1f} s']
        line=f'{self.spin[self.frame%len(self.spin)]} {"".join(parts)}'
        self.frame+=1
        cols=self.width or self._columns()
        line=line[:max(cols-1,10)]
        self._write('\r'+line+' '*max(self.drawn-len(line),0));self.drawn=len(line)

    def _columns(self):
        """Width of the terminal this stream is on (stdout may be piped while stderr is a narrow terminal); else the shutil fallback."""
        try:return os.get_terminal_size(self.stream.fileno()).columns
        except (AttributeError,ValueError,OSError):return shutil.get_terminal_size((80,24)).columns

    def _run(self):
        while not self.stop_event.wait(self.interval):
            with self.lock:self._draw()

    # API
    @contextlib.contextmanager
    def step(self,label):
        """A phase. Top-level steps leave a '✓ label (12.3 s)' line (or '✗' when the body raised); a step inside another only renames the live line."""
        with self.lock:
            step=Step(label,self.clock(),len(self.steps));self.steps.append(step);self.count_text=self.note_text=''
            if self.mode=='plain' and not step.depth:self._write(f'{label} ...\n')
            elif self.mode=='live':
                if self.thread is None:self.thread=threading.Thread(target=self._run,daemon=True,name='tokenatlas-progress');self.thread.start()
                self._draw()
        try:yield step
        except BaseException:step.failed=True;raise
        finally:
            with self.lock:
                self.steps.remove(step);self.count_text=self.note_text=''
                if not step.depth and self.mode!='off':
                    self._clear();self._write(f'{self.cross if step.failed else self.tick} {label} ({self.clock()-step.start:.1f} s)\n' if self.mode=='live'
                                              else f'{label} {"failed" if step.failed else "done"} ({self.clock()-step.start:.1f} s)\n')

    def count(self,done,total,unit='files'):
        with self.lock:self.count_text=f'{done:,}/{total:,} {unit}'

    def note(self,text):
        with self.lock:self.note_text=str(text or '')

    @contextlib.contextmanager
    def suspend(self):
        """Hold the live line off the screen while something else (collect's stdout log lines) prints."""
        with self.lock:self.paused+=1;self._clear()
        try:yield  # the lock is not held here: the redraw thread just skips while paused
        finally:
            with self.lock:self.paused-=1

    def close(self):
        """Stop the redraw thread and clear the live line (idempotent); steps still open end without a line."""
        self.stop_event.set()
        thread,self.thread=self.thread,None
        if thread is not None and thread is not threading.current_thread():thread.join(2)
        with self.lock:self._clear();self.mode='off' if self.mode=='live' else self.mode

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


NOOP=Progress(None,'off')
_current=NOOP


def current():return _current


def start(stream=None,**kw):
    """Make a Progress for stderr the current one unless one is active; True when this call started it (and so should stop it)."""
    global _current
    if _current is not NOOP:return False
    _current=Progress.for_stream(sys.stderr if stream is None else stream,**kw)
    return True


def stop():
    """Close the current Progress (clearing its line) and fall back to the no-op one."""
    global _current
    prog,_current=_current,NOOP
    prog.close()


def finish():
    """Close the live line before a result is printed to stdout; the rest of the command runs silently."""
    if _current.mode=='live':stop()


def step(label):return _current.step(label)
def count(done,total,unit='files'):_current.count(done,total,unit)
def note(text):_current.note(text)
def suspend():return _current.suspend()
