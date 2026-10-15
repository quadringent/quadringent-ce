"""Collect a trusted local producer; process success is not native admission.

Credentials travel through bounded stdin only. The caller supplies the exact
child environment and must separately admit its code, scope and native reads.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from typing import Mapping

from .verification_stream import FrameReader, StreamLimits, StreamSummary


class ProcessCollectionError(ValueError):
    """A bounded collection failed; messages contain no child output or input."""


@dataclass(frozen=True)
class CollectedProcessStream:
    path: Path
    summary: StreamSummary
    return_code: int = 0


def _configuration(command, input_bytes, environment, scope_sha256, dataset, limits):
    if (type(command) is not tuple or not 1 <= len(command) <= 128
            or any(type(arg) is not str or not arg or "\0" in arg or len(arg) > 8192 for arg in command)
            or type(input_bytes) is not bytes or len(input_bytes) > 65536
            or not isinstance(environment, Mapping) or len(environment) > 128):
        raise ProcessCollectionError("invalid_configuration")
    for key, value in environment.items():
        if (type(key) is not str or not key or "=" in key or "\0" in key or len(key) > 256
                or type(value) is not str or "\0" in value or len(value) > 32768):
            raise ProcessCollectionError("invalid_configuration")
    try:
        FrameReader(io.BytesIO(), scope_sha256=scope_sha256, dataset=dataset, limits=limits)
    except ValueError:
        raise ProcessCollectionError("invalid_configuration") from None


def _new_directory(directory):
    path = Path(directory).absolute()
    if ".." in path.parts:
        raise ProcessCollectionError("invalid_directory")
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise ProcessCollectionError("invalid_directory")
    path.mkdir(mode=0o700)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _deadline(started, seconds):
    remaining = seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise ProcessCollectionError("process_deadline")
    return remaining


def _terminate(process):
    if process is None:
        return
    # Descendants can retain stdout after the immediate producer exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def _transfer(process, original, input_bytes, limits, started):
    offset = count = 0
    with selectors.DefaultSelector() as selector:
        for pipe, event, label in ((process.stdout, selectors.EVENT_READ, "stdout"),
                                   (process.stderr, selectors.EVENT_READ, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, event, label)
        if input_bytes:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map():
            for key, _ in selector.select(timeout=min(0.1, _deadline(started, limits.max_seconds))):
                _deadline(started, limits.max_seconds)
                if key.data == "stdin":
                    try:
                        written = os.write(key.fd, memoryview(input_bytes)[offset:offset + 65536])
                    except BrokenPipeError:
                        raise ProcessCollectionError("process_input_incomplete") from None
                    offset += written
                    if offset == len(input_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                elif key.data == "stdout":
                    count += len(chunk)
                    if count > limits.max_bytes:
                        raise ProcessCollectionError("process_byte_budget")
                    original.write(chunk)
                # Drain stderr without persisting potentially sensitive driver output.
        while process.poll() is None:
            try:
                process.wait(timeout=min(0.1, _deadline(started, limits.max_seconds)))
            except subprocess.TimeoutExpired:
                continue
    if offset != len(input_bytes):
        raise ProcessCollectionError("process_input_incomplete")
    if process.returncode != 0:
        raise ProcessCollectionError("process_exit")


def collect_process_stream(command: tuple[str, ...], *, directory: Path, input_bytes: bytes,
                           environment: Mapping[str, str], scope_sha256: str, dataset: str,
                           limits: StreamLimits = StreamLimits()) -> CollectedProcessStream:
    """Archive exact frames only after exit zero, EOF, integrity and durable writes.

    The new private directory's parent must already exist. No shell is used and
    the parent environment is never inherited. Failed originals are removed.
    """
    _configuration(command, input_bytes, environment, scope_sha256, dataset, limits)
    process = None
    path = None
    success = False
    started = time.monotonic()
    try:
        directory = _new_directory(directory)
        path = directory / "original.ndjson"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w+b") as original:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=dict(environment), cwd=directory,
                                       start_new_session=True, close_fds=True)
            _transfer(process, original, input_bytes, limits, started)
            _deadline(started, limits.max_seconds)
            original.flush()
            os.fsync(original.fileno())
            _deadline(started, limits.max_seconds)
            original.seek(0)
            reader = FrameReader(original, scope_sha256=scope_sha256, dataset=dataset, limits=limits)
            try:
                for _row in reader:
                    _deadline(started, limits.max_seconds)
            except ProcessCollectionError:
                raise
            except ValueError:
                raise ProcessCollectionError("invalid_stream") from None
            _deadline(started, limits.max_seconds)
            summary = reader.summary
            info = os.lstat(path)
            if (summary is None or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != (os.fstat(original.fileno()).st_dev,
                                                  os.fstat(original.fileno()).st_ino)):
                raise ProcessCollectionError("original_changed")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _deadline(started, limits.max_seconds)
        result = CollectedProcessStream(path, summary)
        success = True
        return result
    except ProcessCollectionError:
        raise
    except Exception:
        raise ProcessCollectionError("process_collection_failed") from None
    finally:
        if not success:
            _terminate(process)
            if path is not None:
                path.unlink(missing_ok=True)
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
