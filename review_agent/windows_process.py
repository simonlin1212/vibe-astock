"""Windows kernel lifecycle primitives; imported only on Windows.

The guard joins a non-breakaway, kill-on-close Job before starting any child.
Only that guard owns the Job handle, so killing/crashing it also reaps descendants.
Reference: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
import ctypes
from ctypes import wintypes as w

k32 = ctypes.WinDLL('kernel32', use_last_error=True)


def bind(name, args, result):
    fn = getattr(k32, name)
    fn.argtypes, fn.restype = args, result
    return fn


CloseHandle = bind('CloseHandle', [w.HANDLE], w.BOOL)
OpenProcess = bind('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
WaitForSingleObject = bind('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
CreateJobObject = bind('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE)
SetInformationJobObject = bind('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL)
AssignProcessToJobObject = bind('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL)
GetCurrentProcess = bind('GetCurrentProcess', [], w.HANDLE)
TerminateJobObject = bind('TerminateJobObject', [w.HANDLE, w.UINT], w.BOOL)
PeekNamedPipe = bind('PeekNamedPipe', [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                                     ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL)


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount',
        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class GuardJob:
    def __init__(self, parent_pid):
        # Keep an OS handle, not repeated PID lookups (PIDs may be recycled).
        self.parent = OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not self.parent:
            raise ctypes.WinError(ctypes.get_last_error())
        self.job = CreateJobObject(None, None)  # non-inheritable, unnamed
        if not self.job:
            CloseHandle(self.parent)
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no BREAKAWAY
        if (not SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not AssignProcessToJobObject(self.job, GetCurrentProcess())):
            error = ctypes.get_last_error()
            CloseHandle(self.job)
            CloseHandle(self.parent)
            raise ctypes.WinError(error)  # Never launch an unguarded engine.

    def parent_alive(self):
        status = WaitForSingleObject(self.parent, 0)
        if status == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return status == 258  # WAIT_TIMEOUT

    def kill(self):
        if not TerminateJobObject(self.job, 9):
            raise ctypes.WinError(ctypes.get_last_error())
        # This Job includes the guard itself. The OS terminates it here.


def process_alive(pid):
    handle = OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # No such process
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return WaitForSingleObject(handle, 0) == 258
    finally:
        CloseHandle(handle)


def pipe_bytes(pipe):
    """Available bytes, or -1 for EOF. Does not use socket-only select()."""
    import msvcrt
    available = w.DWORD()
    if not PeekNamedPipe(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error in (109, 233):  # BROKEN_PIPE / PIPE_NOT_CONNECTED
            return -1
        raise ctypes.WinError(error)
    return available.value
