"""Retain lifecycle measurements; these receipts grant no native admission."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable

from .verification_collect import ProcessSpec
from .verification_contract import VerificationScope
from .verification_native import NativeCopyInputs, native_journal_processes
from .verification_process import DocumentLimits, collect_process_document

_MAX_DOCUMENT = 4 * 1024**2
_MAX_BUNDLE = 393600
_PINS = ("scope_sha256", "context_sha256", "capture_binding_sha256", "copy_boundary_sha256",
         "copy_run_id", "bundle_sha256")


class NativeGuardError(ValueError):
    """A measurement failed without exposing credentials or producer output."""


@dataclass(frozen=True)
class GuardOriginal:
    path: Path
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class NativeGuardResult:
    journal_original: GuardOriginal
    final_original: GuardOriginal
    stdout_original: GuardOriginal
    metadata: dict


class _Budget:
    def __init__(self, seconds, check_current):
        if type(seconds) is not int or not 1 <= seconds <= 60 or not callable(check_current):
            raise ValueError("invalid_guard_configuration")
        self.seconds, self.check_current = seconds, check_current
        self.started = self.previous = time.monotonic()
        self.check()

    def check(self):
        self._remaining()
        if self.check_current() is not True:
            raise ValueError("native_guard_fence_or_deadline")
        return self._remaining()

    def _remaining(self):
        now = time.monotonic()
        if (type(now) not in (float, int) or not math.isfinite(now) or now < self.previous
                or now - self.started >= self.seconds):
            raise ValueError("native_guard_fence_or_deadline")
        self.previous = now
        return self.seconds - (now - self.started)


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(str(path)) > 4096:
        raise ValueError("unsafe_guard_path")
    return path


def _parent(path):
    directory = os.open(path.parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def _identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("invalid_guard_original")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_original(path, maximum, budget):
    budget.check()
    path = _path(path)
    directory, descriptor = _parent(path), None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = _identity(os.fstat(descriptor))
        if not 1 <= before[2] <= maximum:
            raise ValueError("guard_original_byte_budget")
        raw = bytearray()
        while len(raw) <= maximum:
            budget.check()
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if (len(raw) != before[2] or before != _identity(os.fstat(descriptor))
                or before != _identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False))):
            raise ValueError("guard_original_changed")
        content = bytes(raw)
        budget.check()
        return content, GuardOriginal(path, hashlib.sha256(content).hexdigest(), len(content)), before
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_guard_field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("invalid_guard_number")


def _document(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)


def _guard_document(native, document, mode):
    phases = ["before_business", "after_business"] if mode == "journal" else ["after_all_streams"]
    if (type(document) is not dict
            or document.get("format_version") != "quadringent-native-journal-guard-v1"
            or document.get("mode") != mode or document.get("state") != "complete"
            or document.get("error_code") is not None
            or type(document.get("guards")) is not list
            or any(type(value) is not dict for value in document["guards"])
            or [value.get("phase") for value in document["guards"]] != phases
            or document.get("native_admission") != "not_evaluated"
            or document.get("transaction_consistency_verified") is not False
            or any(document.get(name) != native[name] for name in _PINS)):
        raise ValueError("native_guard_reference_mismatch")
    return document


def _collect_native_receipts(native, producer, *, guard_receipt_path, directory, check_current,
                             max_seconds, collect_document=None, budget=None):
    budget = _Budget(max_seconds, check_current) if budget is None else budget
    guard = _path(guard_receipt_path)
    if (type(producer) is not ProcessSpec or len(producer.command) != 31
            or producer.command[3] != "io.quadringent.as400.NativeBusinessImageExport"):
        raise ValueError("invalid_native_producer")
    for name in _PINS:
        if type(native[name]) is not str or not native[name]:
            raise ValueError("invalid_guard_reference")
        if name.endswith("sha256") and re.fullmatch(r"[0-9a-f]{64}", native[name]) is None:
            raise ValueError("invalid_guard_reference")
    raw, journal, journal_identity = _read_original(guard, _MAX_DOCUMENT, budget)
    _guard_document(native, _document(raw), "journal")
    final_path = guard.parent / "after-all-streams-guard.json"
    if final_path.exists() or final_path.is_symlink():
        raise ValueError("guard_receipt_already_exists")
    command = list(producer.command)
    command[3] = "io.quadringent.as400.NativeLifecycleGuard"
    command[27] = str(final_path)
    seconds = min(60, math.ceil(budget.check()))
    command[30] = str(seconds)
    collect_document = collect_process_document if collect_document is None else collect_document
    result = collect_document(tuple(command), directory=_path(directory), input_bytes=producer.input_bytes,
        environment=producer.environment, validate=lambda value: _guard_document(native, value, "after_all_streams"),
        limits=DocumentLimits(max_bytes=_MAX_DOCUMENT, max_seconds=seconds))
    budget.check()
    stdout_raw, stdout, _ = _read_original(result.path, _MAX_DOCUMENT, budget)
    final_raw, final, _ = _read_original(final_path, _MAX_DOCUMENT, budget)
    _guard_document(native, _document(stdout_raw), "after_all_streams")
    _guard_document(native, _document(final_raw), "after_all_streams")
    if (type(result.return_code) is not int or result.return_code != 0 or stdout_raw != final_raw
            or result.sha256 != stdout.sha256 or result.byte_count != stdout.byte_count):
        raise ValueError("native_final_guard_differs")
    current_raw, current, current_identity = _read_original(guard, _MAX_DOCUMENT, budget)
    if current != journal or current_raw != raw or current_identity != journal_identity:
        raise ValueError("native_journal_guard_changed")
    budget.check()
    metadata = {name: native[name] for name in _PINS}
    metadata.update(native_admission="not_evaluated", guard_receipt_sha256=journal.sha256,
                    guard_receipt_bytes=journal.byte_count,
                    final_guard=dict(mode="after_all_streams", path=str(final.path), original_path=str(stdout.path),
                                     sha256=stdout.sha256, byte_count=stdout.byte_count))
    return NativeGuardResult(journal, final, stdout, deepcopy(metadata))


def collect_native_guards(copy: NativeCopyInputs, scope: VerificationScope, producer: ProcessSpec, *,
                          guard_receipt_path: Path, directory: Path, check_current: Callable,
                          max_seconds: int = 60) -> NativeGuardResult:
    """Call after seven-stream collection; validate measurement bytes, not native authority."""
    try:
        budget = _Budget(max_seconds, check_current)
        if type(copy) is not NativeCopyInputs or type(scope) is not VerificationScope or type(producer) is not ProcessSpec:
            raise ValueError("invalid_native_guard_inputs")
        command = producer.command
        if len(command) != 31 or command[27] != str(_path(guard_receipt_path)):
            raise ValueError("invalid_native_guard_invocation")
        bundle_path = _path(command[25])
        expected = native_journal_processes(copy, scope, java=command[0], classpath=command[2],
            credentials=producer.input_bytes, environment=producer.environment, bundle_path=bundle_path,
            guard_receipt_path=_path(guard_receipt_path), max_seconds=int(command[30]))[0].process
        if expected.command != command or hashlib.sha256(copy.bundle).hexdigest() != copy.bundle_sha256:
            raise ValueError("invalid_native_guard_invocation")
        bundle, original, identity = _read_original(bundle_path, _MAX_BUNDLE, budget)
        if bundle != copy.bundle or original.sha256 != copy.bundle_sha256:
            raise ValueError("native_guard_bundle_changed")
        pins = dict(scope_sha256=scope.sha256, context_sha256=copy.context_sha256,
                    capture_binding_sha256=copy.binding_sha256, copy_boundary_sha256=copy.boundary_sha256,
                    copy_run_id=scope.copy_run_id, bundle_sha256=copy.bundle_sha256)
        result = _collect_native_receipts(pins, producer, guard_receipt_path=guard_receipt_path,
            directory=directory, check_current=check_current, max_seconds=max_seconds, budget=budget)
        after, current, observed = _read_original(bundle_path, _MAX_BUNDLE, budget)
        if current != original or after != bundle or observed != identity:
            raise ValueError("native_guard_bundle_changed")
        budget.check()
        return result
    except Exception:
        raise NativeGuardError("native_guard_failed") from None
