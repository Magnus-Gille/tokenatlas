"""Windows installer containment: assign a waiting helper before it starts work.

The job kills descendants if the supervising CLI exits, including abnormal exit.
No process arguments, environment or unrelated process inventory is inspected.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import json


class _Limits(ctypes.Structure):
    _fields_ = [('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),
                ('flags',wintypes.DWORD),('min_working_set',ctypes.c_size_t),
                ('max_working_set',ctypes.c_size_t),('active_limit',wintypes.DWORD),
                ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic',_Limits),('io',ctypes.c_uint64*6),
                ('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                ('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('times',ctypes.c_int64*4),('faults',wintypes.DWORD),
                ('total',wintypes.DWORD),('active',wintypes.DWORD),('terminated',wintypes.DWORD)]


class _Job:
    def __init__(self):
        self.api = ctypes.WinDLL('kernel32',use_last_error=True)
        api = self.api
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE,wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.handle = api.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, proc):
        if not self.api.AssignProcessToJobObject(self.handle,int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self):
        info = _Accounting()
        if not self.api.QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def drain(self):
        # If termination fails, retain the caller's installation lock until the
        # job finishes naturally. Never turn a failed taskkill into an unlock.
        if self.active():
            stopped = self.api.TerminateJobObject(self.handle,1)
            error = None if stopped else ctypes.WinError(ctypes.get_last_error())
            while self.active():time.sleep(.05)
            if error:raise error

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


_HELPER = '''import json, subprocess, sys
argv = json.loads(sys.argv[1])
if sys.stdin.readline() != "start\\n":
    raise SystemExit(2)
raise SystemExit(subprocess.run(argv, stdin=subprocess.DEVNULL).returncode)
'''


def execute_windows(argv, env, cwd, timeout):
    """Keep all child processes in one kill-on-close job until fully drained."""
    python = Path(getattr(sys,'_base_executable',sys.executable)).resolve()
    if sys.prefix != sys.base_prefix and python.is_relative_to(Path(sys.prefix).resolve()):
        raise OSError('cannot identify an external Python for safe Windows installer supervision')
    job = _Job()
    proc = None
    handlers = {}
    def interrupted(signum,frame):raise KeyboardInterrupt
    try:
        for name in ('SIGINT','SIGTERM','SIGHUP'):
            if hasattr(signal,name):
                sig = getattr(signal,name)
                handlers[sig] = signal.signal(sig,interrupted)
        proc = subprocess.Popen([str(python),'-I','-S','-c',_HELPER,json.dumps(list(map(str,argv)))],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                text=True,errors='replace',env=env,cwd=cwd,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        job.assign(proc)
        out, err = proc.communicate(input='start\n',timeout=timeout)
        return subprocess.CompletedProcess(argv,proc.returncode,out,err)
    finally:
        # Defer further interruption until cleanup, so neither Ctrl-C nor a
        # second termination request releases the lock while children run.
        for sig in handlers:signal.signal(sig,signal.SIG_IGN)
        try:
            if proc is not None:
                if proc.stdin and not proc.stdin.closed:proc.stdin.close()
                job.drain()
                # Assignment may have failed: that helper is still waiting and
                # has never received the start handshake.
                if proc.poll() is None:proc.kill()
                proc.wait()
                if proc.stdout:proc.stdout.close()
                if proc.stderr:proc.stderr.close()
        finally:
            job.close()
            for sig,handler in handlers.items():signal.signal(sig,handler)
