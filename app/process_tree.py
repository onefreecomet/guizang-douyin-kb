"""Own the worker and all descendants; Windows closes the job on server exit."""
import os
import signal
import subprocess

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL


class ProcessTree:
    def __init__(self, cmd, *, env, cwd, combine_output=True):
        self.handle = None
        self.proc = None
        try:
            if os.name == 'nt':
                self.handle = kernel.CreateJobObjectW(None, None)
                if not self.handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                limits = EXTENDED_LIMIT()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
                if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise ctypes.WinError(ctypes.get_last_error())
            env = dict(env, DY_START_GATE='1')
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT if combine_output else subprocess.PIPE, text=True, encoding='utf-8',
                                         errors='replace', bufsize=1, env=env, cwd=str(cwd),
                                         start_new_session=os.name != 'nt',
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if self.handle and not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(self.proc._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
            self.proc.stdin.write('GO\n')
            self.proc.stdin.flush()
            self.proc.stdin.close()
            self.proc.stdin = None
        except Exception:
            if self.proc is not None:
                self.proc.kill()
                self.proc.wait()
            self.close()
            raise

    def terminate(self):
        if os.name == 'nt' and self.handle:
            if not kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        elif self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.proc is not None:
            self.proc.wait(timeout=15)

    def close(self):
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle = None
