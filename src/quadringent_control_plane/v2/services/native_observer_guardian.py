"""Own Linux worker descendants without changing the API process's reaper state."""

from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .native_observer_process import _children, _identity, _signal, _tree


def _subreaper():
    if sys.platform != "linux":
        raise ValueError("guardian_unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    observed = ctypes.c_int()
    if prctl(36, 1, 0, 0, 0) != 0 or prctl(37, ctypes.addressof(observed), 0, 0, 0) != 0 or observed.value != 1:
        raise ValueError("guardian_unavailable")
    if _identity(os.getpid()) is None:
        raise ValueError("guardian_unavailable")
    Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text()


def _cleanup(process):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        # Direct children also include orphans adopted from an exited worker.
        for child in tuple(_children(os.getpid())):
            _signal(child, _identity(child), signal.SIGSTOP)
            try:
                found = _tree(child)
            except ValueError:
                found = {child: _identity(child)}
            for pid, identity in found.items():
                _signal(pid, identity, signal.SIGSTOP)
            for pid, identity in reversed(tuple(found.items())):
                _signal(pid, identity, signal.SIGKILL)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid == 0:
                break
        time.sleep(0.005)
    return False


def supervise(command, *, max_seconds):
    process = None
    stopped = False
    exit_code = 1
    deadline = None

    def stop(_number, _frame):
        nonlocal stopped
        stopped = True

    try:
        if not (type(command) is tuple and command and all(type(v) is str and v and "\0" not in v for v in command)
                and type(max_seconds) in {int, float} and math.isfinite(max_seconds) and 0 < max_seconds <= 7200):
            return 1
        deadline = time.monotonic() + max_seconds
        _subreaper()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # Inherit the pipes without inspecting or copying their secret-bearing data.
        process = subprocess.Popen(command, close_fds=True)
        while not stopped and time.monotonic() < deadline:
            result = process.poll()
            if result is not None:
                exit_code = result if 0 <= result <= 255 else 1
                break
            time.sleep(0.005)
    except Exception:
        exit_code = 1
    finally:
        if process is not None and not _cleanup(process):
            exit_code = 1
    return exit_code if not stopped and deadline is not None and time.monotonic() < deadline else 1


def main():
    try:
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            return 1
        return supervise(tuple(sys.argv[3:]), max_seconds=float(sys.argv[1]))
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
