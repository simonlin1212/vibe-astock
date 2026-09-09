"""Bounded binary pipe transport for POSIX and Windows; no shell invocation."""
import os
import select
import threading
import time

from .evidence import EvidenceError

GUARD_REAP_EXIT = 9 if os.name == 'nt' else -9


def read_chunk(pipe, timeout=.1):
    """Return bytes, b'' at EOF, or None when no data arrived this interval."""
    if os.name != 'nt':
        if not select.select([pipe], [], [], timeout)[0]:
            return None
        return os.read(pipe.fileno(), 65536)
    from .windows_process import pipe_bytes
    deadline = time.monotonic() + timeout
    while True:
        count = pipe_bytes(pipe)
        if count < 0:
            return b''
        if count:
            return os.read(pipe.fileno(), min(count, 65536))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(.02, remaining))


def write_input(proc, payload: bytes, cancel, deadline):
    """Keep cancellation/deadline responsive even when the child stops reading.

    Windows Python 3.10/3.11 do not support nonblocking anonymous pipe writes.
    A bounded writer thread is joined after killing the guarded process on error.
    """
    done = threading.Event()
    errors = []

    def write():
        try:
            remaining = memoryview(payload)
            while remaining:
                size = proc.stdin.write(remaining)
                if not size:
                    raise OSError('pipe closed')
                remaining = remaining[size:]
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            done.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        while True:
            if cancel.is_set():
                raise EvidenceError('任务已取消')
            if time.monotonic() >= deadline:
                raise EvidenceError('引擎输入超时，已停止')
            if done.wait(.05):
                if errors:
                    raise EvidenceError('引擎未接收完整问题，任务已停止')
                return
    except BaseException:
        from .runtime import stop_process
        stop_process(proc)
        raise
    finally:
        writer.join(timeout=2)
