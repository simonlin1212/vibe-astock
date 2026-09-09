"""Independent deadline survives a backend crash (POSIX process groups / Windows Job Objects)."""
import os
import json
import signal
import subprocess
import sys
import time


def guard_python() -> str:
    """Run the stdlib-only guard directly, without Windows venv's extra process.

    CPython's Windows venv redirector changes the actual parent PID and makes
    Popen refer to the redirector, not the process that owns the Job handle.
    Workload children still use the caller's venv interpreter and dependencies.
    """
    return getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable


def main() -> int:
    timeout = float(sys.argv[1])
    parent = int(sys.argv[2])
    if os.getppid() != parent:
        return 1
    job = None
    if os.name == "nt":
        # Script entry has this directory on sys.path, even with cwd outside repo.
        from windows_process import GuardJob
        job = GuardJob(parent)

    def parent_alive():
        return job.parent_alive() if job else os.getppid() == parent

    def kill_tree():
        if job:
            job.kill()
        else:
            os.killpg(os.getpgrp(), signal.SIGKILL)

    if not parent_alive():
        return 1
    reap_group = sys.argv[3] == "--bridge-reap-group"
    child = subprocess.Popen(sys.argv[4:] if reap_group else sys.argv[3:])
    deadline = time.monotonic() + timeout if timeout > 0 else float("inf")
    while child.poll() is None:
        if time.monotonic() > deadline or not parent_alive():
            kill_tree()
        time.sleep(.2)
    if reap_group:
        # A private bridge result is followed by the child's actual exit status.
        # The owner waits for our death before accepting it. Always reap the
        # entire group, even if EOF made Node exit before we noticed parent death.
        try:
            print(json.dumps({"type": "bridge_exit", "code": child.returncode}), flush=True)
        finally:
            kill_tree()
    if not parent_alive():
        kill_tree()
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
