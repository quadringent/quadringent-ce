"""Supervise collector I/O outside the process that may block in an SDK."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from queue import Queue
import selectors
import signal
import subprocess
import sys
import threading
import time


class NativeObserverProcessError(ValueError):
    """A bounded error which never contains worker inputs or diagnostics."""


def _require(value):
    if not value:
        raise ValueError("invalid_observation_worker")


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value)
            value[key] = item
        return value

    def invalid(_):
        raise ValueError("invalid_observation_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def bounded_call(function, *, max_seconds):
    """Bound a control-database read; a timed-out daemon performs no further calls."""
    answer = Queue(maxsize=1)

    def invoke():
        try:
            answer.put((True, function()))
        except Exception:
            answer.put((False, None))

    try:
        _require(type(max_seconds) in {int, float} and math.isfinite(max_seconds) and max_seconds > 0)
        deadline = time.monotonic() + max_seconds
        threading.Thread(target=invoke, daemon=True, name="native-configuration-read").start()
        ok, value = answer.get(timeout=max_seconds)
        _require(ok and time.monotonic() < deadline)
        return value
    except Exception:
        raise NativeObserverProcessError("native_observation_failed") from None


class _Fence:
    def __init__(self, callback):
        self.requests, self.answers = Queue(maxsize=1), Queue(maxsize=1)
        self.stopped = threading.Event()

        def worker():
            while not self.stopped.is_set():
                item = self.requests.get()
                if item is None or self.stopped.is_set():
                    return
                try:
                    answer = callback() is True
                except Exception:
                    answer = False
                self.answers.put(answer)

        threading.Thread(target=worker, daemon=True, name="native-context-check").start()

    def check(self, deadline):
        self.requests.put(True, timeout=max(0.001, deadline - time.monotonic()))
        result = self.answers.get(timeout=max(0.001, deadline - time.monotonic()))
        _require(result and time.monotonic() < deadline)

    def close(self):
        self.stopped.set()
        try:
            self.requests.put_nowait(None)
        except Exception:
            pass


def _identity(pid):
    try:
        # The start tick prevents signaling a PID which has since been reused.
        values = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        return values[19]
    except (OSError, IndexError):
        return None


def _children(pid):
    result = set()
    try:
        for path in Path(f"/proc/{pid}/task").glob("*/children"):
            result.update(int(value) for value in path.read_text().split())
    except OSError:
        pass
    return result


def _tree(pid):
    found = {}
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        identity = _identity(current)
        if identity is not None:
            found[current] = identity
            _require(len(found) <= 1024)
            pending.extend(_children(current))
    return found


def _signal(pid, identity, kind):
    if identity is not None and _identity(pid) == identity:
        try:
            os.kill(pid, kind)
        except ProcessLookupError:
            pass


def _terminate(process, known):
    if process is None:
        return
    if sys.platform == "linux" and process.poll() is None:
        # Let the isolated subreaper dispose of adopted Java sessions first.
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    elif process.poll() is None:
        if os.name == "posix" and Path("/proc").is_dir():
            _signal(process.pid, known.get(process.pid), signal.SIGSTOP)
            # Freeze descendants before killing the worker, including Java's own sessions.
            for _ in range(4):
                try:
                    found = _tree(process.pid)
                except ValueError:
                    found = {}
                added = found.keys() - known.keys()
                known.update(found)
                for pid, identity in found.items():
                    _signal(pid, identity, signal.SIGSTOP)
                if not added:
                    break
        else:
            try:
                os.killpg(process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
    for pid, identity in reversed(tuple(known.items())):
        _signal(pid, identity, signal.SIGKILL)
    # A process group can survive the exit of its original session leader.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def run_worker(command, payload, *, environment, check_current, max_seconds):
    """Exchange bounded JSON and context fences with one server-owned worker."""
    process = fence = None
    known = {}
    try:
        _require(os.name == "posix")
        _require(type(command) is tuple and command and all(type(v) is str and v and "\0" not in v for v in command))
        _require(
            type(max_seconds) in {int, float}
            and math.isfinite(max_seconds)
            and 0 < max_seconds <= 7200
            and callable(check_current)
            and type(payload) is dict
        )
        deadline = time.monotonic() + max_seconds
        fence = _Fence(check_current)
        fence.check(deadline)
        initial = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode()
        _require(len(initial) <= 4 * 1024**2)
        guarded = command
        if sys.platform == "linux":
            guarded = (sys.executable, "-P", "-u", "-m", "quadringent_control_plane.v2.services.native_observer_guardian",
                       str(max(0.001, deadline - time.monotonic())), "--", *command)
        process = subprocess.Popen(
            guarded,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            start_new_session=True,
            close_fds=True,
        )
        known = _tree(process.pid) if Path("/proc").is_dir() else {}
        output = bytearray()
        pending = bytearray(initial)
        result = None
        index = 0
        total = 0
        eof = False
        _require(process.stdin is not None and process.stdout is not None)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            while not eof:
                remaining = deadline - time.monotonic()
                _require(remaining > 0)
                if Path("/proc").is_dir():
                    known.update(_tree(process.pid))
                events = selector.select(min(0.2, remaining))
                for key, _ in events:
                    if key.data == "in":
                        count = os.write(process.stdin.fileno(), pending[:65536])
                        del pending[:count]
                        if not pending:
                            selector.unregister(process.stdin)
                        continue
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        eof = True
                        break
                    total += len(data)
                    _require(total <= 16 * 1024**2)
                    output.extend(data)
                    _require(len(output) <= 8 * 1024**2)
                    while b"\n" in output:
                        line, _, rest = output.partition(b"\n")
                        output = bytearray(rest)
                        message = _decode(line)
                        _require(type(message) is dict and result is None)
                        if message.get("kind") == "fence":
                            _require(
                                set(message) == {"kind", "index"}
                                and type(message["index"]) is int
                                and message["index"] == index + 1
                                and index < 100_000
                            )
                            index += 1
                            fence.check(deadline)
                            answer = {"current": True, "index": index}
                        else:
                            _require(
                                set(message) == {"kind", "value"}
                                and message["kind"] == "result"
                                and type(message["value"]) is dict
                                and index > 0
                            )
                            result = message["value"]
                            fence.check(deadline)
                            answer = {"finish": True}
                        _require(not pending)
                        pending.extend((json.dumps(answer, separators=(",", ":")) + "\n").encode())
                        selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            _require(not output and not pending and result is not None)
            _require(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0)
            fence.check(deadline)
            return result
    except Exception:
        raise NativeObserverProcessError("native_observation_failed") from None
    finally:
        if fence is not None:
            fence.close()
        _terminate(process, known)
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
