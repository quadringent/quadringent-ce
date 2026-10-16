"""Collect seven original streams; equality is not native admission.

The caller admits producer code, the physical read-set and source context.
Blocking destination/store calls still require a supervised outer deadline.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
from types import MappingProxyType
from typing import Callable, Mapping

from .verification_contract import ReceiverWindow, VerificationScope
from .verification_destination import export_destination
from .verification_ledger import DiskReconciler
from .verification_process import collect_process_stream
from .verification_stored import ObjectReference, StoredLimits, StoredReadSet, export_stored
from .verification_stream import FrameReader, StreamLimits, StreamSummary, write_stream


class CollectionError(ValueError):
    """Collection failed without exposing producer inputs or messages."""


@dataclass(frozen=True)
class ProcessSpec:
    command: tuple[str, ...] = field(repr=False)
    input_bytes: bytes = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)

    def __post_init__(self):
        if (type(self.command) is not tuple or not 1 <= len(self.command) <= 128
                or any(type(arg) is not str or not arg or '\0' in arg or len(arg) > 8192 for arg in self.command)
                or type(self.input_bytes) is not bytes or len(self.input_bytes) > 65536
                or not isinstance(self.environment, Mapping) or len(self.environment) > 128):
            raise CollectionError('invalid_process_spec')
        environment = dict(self.environment)
        for key, value in environment.items():
            if (type(key) is not str or not key or '=' in key or '\0' in key or len(key) > 256
                    or type(value) is not str or '\0' in value or len(value) > 32768):
                raise CollectionError('invalid_process_spec')
        object.__setattr__(self, 'environment', MappingProxyType(environment))


@dataclass(frozen=True)
class WindowProcess:
    window: ReceiverWindow
    process: ProcessSpec

    def __post_init__(self):
        if type(self.window) is not ReceiverWindow or type(self.process) is not ProcessSpec:
            raise CollectionError('invalid_window_process')


@dataclass(frozen=True)
class StreamReference:
    path: Path
    summary: StreamSummary


@dataclass(frozen=True)
class OriginalReference:
    path: Path
    sha256: str
    byte_count: int
    dataset: str | None = None
    window: ReceiverWindow | None = None
    object_key: str | None = None
    stream_summary: StreamSummary | None = None
    process_return_code: int | None = None


@dataclass(frozen=True)
class CollectedVerification:
    report: dict
    streams: Mapping[str, StreamReference]
    originals: tuple[OriginalReference, ...]
    report_reference: OriginalReference
    collection_reference: OriginalReference


@dataclass(frozen=True)
class _Seal:
    reference: OriginalReference
    identity: tuple[int, ...]


class _Budget:
    def __init__(self, seconds, check_current):
        self.started = self.previous = time.monotonic()
        self.seconds, self.check_current = seconds, check_current

    def remaining(self):
        now = time.monotonic()
        remaining = self.seconds - (now - self.started)
        if not math.isfinite(now) or now < self.previous or remaining <= 0:
            raise CollectionError('collection_deadline')
        self.previous = now
        return remaining

    def admit(self):
        self.remaining()
        if self.check_current() is not True:
            raise CollectionError('configuration_superseded')
        self.remaining()

    def limits(self, limits):
        seconds = min(limits.max_seconds, math.floor(self.remaining()))
        if seconds < 1:
            raise CollectionError('collection_deadline')
        return replace(limits, max_seconds=seconds)


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise CollectionError('unsafe_collection_directory')
    path.mkdir(mode=0o700)
    created = os.lstat(path)
    try:
        os.chmod(path, 0o700)
        _sync(path.parent)
    except Exception:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            path.rmdir()
        raise
    return path


def _identity(info):
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise CollectionError('original_changed')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open(seal):
    path = seal.reference.path
    if any(part.is_symlink() for part in path.parents):
        raise CollectionError('original_changed')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(descriptor)) != seal.identity or _identity(os.lstat(path)) != seal.identity:
            raise CollectionError('original_changed')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _verify(seal, budget):
    digest, count = hashlib.sha256(), 0
    with _open(seal) as stream:
        while True:
            budget.remaining()
            chunk = stream.read(65536)
            if not chunk:
                break
            count += len(chunk)
            if count > seal.reference.byte_count:
                raise CollectionError('original_changed')
            digest.update(chunk)
        if _identity(os.fstat(stream.fileno())) != seal.identity:
            raise CollectionError('original_changed')
    if count != seal.reference.byte_count or digest.hexdigest() != seal.reference.sha256:
        raise CollectionError('original_changed')


def _seal(path, budget, *, dataset=None, window=None, object_key=None, summary=None, limits=None, scope=None,
          process_return_code=None):
    info = os.lstat(path)
    identity = _identity(info)
    digest, count = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as original:
        if _identity(os.fstat(original.fileno())) != identity:
            raise CollectionError('original_changed')
        if summary is not None:
            reader = FrameReader(original, scope_sha256=scope.sha256, dataset=dataset, limits=budget.limits(limits))
            for row in reader:
                budget.remaining()
                if window is not None:
                    _window_row(row, window)
            if reader.summary != summary:
                raise CollectionError('sealed_stream_differs')
            digest_value, count = summary.sha256, summary.byte_count
        else:
            while chunk := original.read(65536):
                budget.remaining()
                digest.update(chunk)
                count += len(chunk)
            digest_value = digest.hexdigest()
        if _identity(os.fstat(original.fileno())) != identity or _identity(os.lstat(path)) != identity:
            raise CollectionError('original_changed')
    if info.st_size != count:
        raise CollectionError('original_changed')
    seal = _Seal(OriginalReference(path, digest_value, count, dataset, window, object_key, summary, process_return_code), identity)
    _verify(seal, budget)
    return seal


def _write(path, producer, budget, *, dataset=None, scope=None, limits=None, object_key=None):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w+b') as output:
        os.fchmod(output.fileno(), 0o600)
        summary = producer(output)
        output.flush()
        os.fsync(output.fileno())
        budget.remaining()
    _sync(path.parent)
    if dataset is not None and type(summary) is not StreamSummary:
        raise CollectionError('producer_completion_missing')
    seal = _seal(path, budget, dataset=dataset, scope=scope, summary=summary if dataset else None,
                 limits=limits, object_key=object_key)
    return seal, summary


def _window_row(row, window):
    sequence = row.get('JOURNAL_SEQUENCE')
    if (row.get('JOURNAL_RECEIVER') != window.identity.receiver_name
            or type(sequence) not in (str, int)
            or re.fullmatch(r'0|[1-9][0-9]*', str(sequence)) is None
            or not window.start_sequence <= int(sequence) <= window.end_sequence):
        raise CollectionError('producer_window_differs')


def _reference_record(reference, root):
    return {'path': reference.path.relative_to(root).as_posix(), 'sha256': reference.sha256,
            'byte_count': reference.byte_count, 'dataset': reference.dataset,
            'window': reference.window.to_dict() if reference.window else None,
            'object_key': reference.object_key,
            'stream_summary': reference.stream_summary.to_dict() if reference.stream_summary else None,
            'process_return_code': reference.process_return_code}


def _inventory(root, scope, streams, originals, comparison, budget, limits):
    record = {
        'format_version': 'quadringent-native-table-collection-v1', 'scope_sha256': scope.sha256,
        'native_admission': 'not_evaluated', 'native_cdc_qualified': False,
        'context_enforcement': 'caller_configuration_fence_only',
        'comparison': _reference_record(comparison, root),
        'streams': {dataset: {'path': reference.path.relative_to(root).as_posix(),
                              'sha256': reference.summary.sha256, 'byte_count': reference.summary.byte_count,
                              'summary': reference.summary.to_dict()}
                    for dataset, reference in streams.items()},
        'originals': [_reference_record(reference, root) for reference in originals],
    }
    digest, count = hashlib.sha256(), 0

    def write(output):
        nonlocal count
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        for text in encoder.iterencode(record):
            data = text.encode('ascii')
            budget.remaining()
            count += len(data)
            if count >= limits.max_bytes:
                raise CollectionError('inventory_byte_budget')
            if output.write(data) != len(data):
                raise CollectionError('inventory_write_incomplete')
            digest.update(data)
        if output.write(b'\n') != 1:
            raise CollectionError('inventory_write_incomplete')
        digest.update(b'\n')
        count += 1

    seal, _ = _write(root / 'collection.json', write, budget)
    if seal.reference.byte_count != count or seal.reference.sha256 != digest.hexdigest():
        raise CollectionError('inventory_archive_differs')
    return seal


class _RecordingStore:
    def __init__(self, store, directory, limits, budget, seals, originals):
        self.store, self.directory, self.limits, self.budget = store, directory, limits, budget
        self.seals, self.originals = seals, originals
        self.seen = {}
        self.bytes = self.reads = 0

    def get_bounded(self, key, maximum):
        self.budget.remaining()
        if self.reads >= self.limits.max_objects or self.bytes >= self.limits.max_total_bytes:
            raise CollectionError('stored_collection_budget')
        maximum = min(maximum, self.limits.max_object_bytes, self.limits.max_total_bytes - self.bytes)
        data = self.store.get_bounded(key, maximum)
        self.budget.remaining()
        if type(data) is not bytes or not 0 < len(data) <= maximum:
            raise CollectionError('stored_collection_budget')
        self.reads += 1
        self.bytes += len(data)
        digest = hashlib.sha256(data).hexdigest()
        if key in self.seen:
            if self.seen[key] != digest:
                raise CollectionError('stored_original_changed')
        else:
            path = self.directory / f'object-{len(self.seen):06d}.original'
            seal, _ = _write(path, lambda output: output.write(data), self.budget, object_key=key)
            if seal.reference.byte_count != len(data) or seal.reference.sha256 != digest:
                raise CollectionError('stored_archive_differs')
            self.seals.append(seal)
            self.originals.append(seal.reference)
            self.seen[key] = digest
        return data


def collect_verification(directory, scope: VerificationScope, *, source: ProcessSpec,
                         journal: tuple[WindowProcess, ...], positions: tuple[WindowProcess, ...],
                         store, raw_prefix_root: str, initial_copy: ObjectReference,
                         copy_intent: ObjectReference, raw_read_set: StoredReadSet,
                         connection_factory: Callable, check_current: Callable,
                         limits: StreamLimits = StreamLimits(), stored_limits: StoredLimits = StoredLimits(),
                         max_seconds: int = 3600) -> CollectedVerification:
    """Return only complete archives and comparisons, including divergent results.

    check_current is a configuration fence, not native admission. Each factory
    connection is owned and closed here. Process credentials stay on stdin.
    """
    root = root_identity = None
    success = False
    try:
        if (type(scope) is not VerificationScope or type(source) is not ProcessSpec
                or type(limits) is not StreamLimits or type(stored_limits) is not StoredLimits
                or type(max_seconds) is not int or not 1 <= max_seconds <= 7200
                or type(initial_copy) is not ObjectReference or type(copy_intent) is not ObjectReference
                or type(raw_read_set) is not StoredReadSet or raw_read_set.scope_sha256 != scope.sha256
                or not callable(connection_factory) or not callable(check_current)
                or not callable(getattr(store, 'get_bounded', None))):
            raise CollectionError('invalid_collection_configuration')
        for specs in (journal, positions):
            if (type(specs) is not tuple or any(type(spec) is not WindowProcess for spec in specs)
                    or tuple(spec.window for spec in specs) != scope.receiver_windows):
                raise CollectionError('producer_windows_differ')
        budget = _Budget(max_seconds, check_current)
        budget.admit()
        root = _mkdir(directory)
        info = root.stat()
        root_identity = info.st_dev, info.st_ino
        streams, seals, originals = {}, [], []

        def process(spec, dataset, path, window=None, custom_limits=None):
            budget.admit()
            result = collect_process_stream(spec.command, directory=path, input_bytes=spec.input_bytes,
                                            environment=spec.environment, scope_sha256=scope.sha256,
                                            dataset=dataset, limits=budget.limits(custom_limits or limits))
            if type(result.return_code) is not int or result.return_code != 0:
                raise CollectionError('producer_exit_missing')
            seal = _seal(result.path, budget, dataset=dataset, window=window, summary=result.summary,
                         limits=limits, scope=scope, process_return_code=result.return_code)
            seals.append(seal)
            originals.append(seal.reference)
            return seal, result.summary

        seal, summary = process(source, 'source', root / 'source')
        streams['source'] = StreamReference(seal.reference.path, summary)
        for dataset, specs in (('journal', journal), ('positions', positions)):
            parent = _mkdir(root / dataset)
            windows, rows, byte_count = [], 0, 0
            for index, spec in enumerate(specs):
                remaining_bytes = limits.max_bytes - byte_count
                if remaining_bytes < 1:
                    raise CollectionError('window_byte_budget')
                bounded = replace(limits, max_rows=max(1, limits.max_rows - rows), max_bytes=remaining_bytes)
                item, summary = process(spec.process, dataset, parent / f'window-{index:03d}', spec.window, bounded)
                windows.append((item, summary))
                rows += summary.row_count
                byte_count += summary.byte_count
                if rows > limits.max_rows or byte_count > limits.max_bytes:
                    raise CollectionError('window_population_budget')

            def merged_rows():
                for item, expected in windows:
                    budget.admit()
                    _verify(item, budget)
                    with _open(item) as original:
                        reader = FrameReader(original, scope_sha256=scope.sha256, dataset=dataset,
                                             limits=budget.limits(limits))
                        for row in reader:
                            budget.remaining()
                            _window_row(row, item.reference.window)
                            yield row
                        if reader.summary != expected:
                            raise CollectionError('original_changed')

            seal, summary = _write(parent / 'merged.ndjson', lambda output: write_stream(
                output, merged_rows(), scope_sha256=scope.sha256, dataset=dataset,
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=lambda: datetime.now(timezone.utc).isoformat(), limits=budget.limits(limits)),
                budget, dataset=dataset, scope=scope, limits=limits)
            seals.append(seal)
            streams[dataset] = StreamReference(seal.reference.path, summary)

        objects = _mkdir(root / 'stored-originals')
        recording = _RecordingStore(store, objects, stored_limits, budget, seals, originals)
        for dataset in ('snapshot', 'raw'):
            budget.admit()
            parent = _mkdir(root / dataset)
            seal, summary = _write(parent / 'original.ndjson', lambda output: export_stored(
                recording, scope, output, dataset=dataset, raw_prefix_root=raw_prefix_root,
                initial_copy=initial_copy, copy_intent=copy_intent,
                raw_read_set=raw_read_set if dataset == 'raw' else None,
                limits=budget.limits(limits), stored_limits=stored_limits),
                budget, dataset=dataset, scope=scope, limits=limits)
            seals.append(seal)
            originals.append(seal.reference)
            streams[dataset] = StreamReference(seal.reference.path, summary)

        for dataset in ('history', 'mirror'):
            budget.admit()
            parent = _mkdir(root / dataset)

            def destination(output):
                connection = connection_factory()
                try:
                    return export_destination(connection, scope, output, dataset=dataset,
                                              limits=budget.limits(limits))
                finally:
                    connection.close()

            seal, summary = _write(parent / 'original.ndjson', destination, budget,
                                   dataset=dataset, scope=scope, limits=limits)
            seals.append(seal)
            originals.append(seal.reference)
            streams[dataset] = StreamReference(seal.reference.path, summary)

        budget.admit()
        for seal in seals:
            _verify(seal, budget)
        with DiskReconciler(root / 'ledger', scope, max_seconds=min(7200, math.floor(budget.remaining()))) as ledger:
            for dataset in ('source', 'snapshot', 'journal', 'positions', 'raw', 'history', 'mirror'):
                reference = streams[dataset]
                item = next(seal for seal in seals if seal.reference.path == reference.path)
                _verify(item, budget)
                with _open(item) as original:
                    reader = FrameReader(original, scope_sha256=scope.sha256, dataset=dataset,
                                         limits=budget.limits(limits))
                    ledger.ingest(dataset, reader)
                    if reader.summary != reference.summary:
                        raise CollectionError('original_changed')
            report = ledger.finish()
        budget.admit()
        if report['state'] not in {'verified', 'divergent'}:
            raise CollectionError('comparison_incomplete')
        for seal in seals:
            _verify(seal, budget)
        report_bytes = (json.dumps(report, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                                   allow_nan=False) + '\n').encode()
        report_seal, _ = _write(root / 'comparison.json', lambda output: output.write(report_bytes), budget)
        if (report_seal.reference.byte_count != len(report_bytes)
                or report_seal.reference.sha256 != hashlib.sha256(report_bytes).hexdigest()):
            raise CollectionError('report_archive_differs')
        seals.append(report_seal)
        collection_seal = _inventory(root, scope, streams, originals, report_seal.reference, budget, limits)
        seals.append(collection_seal)
        _sync(root / 'ledger')
        _sync(root)
        budget.admit()
        for seal in seals:
            _verify(seal, budget)
        budget.admit()
        success = True
        return CollectedVerification(report, MappingProxyType(streams), tuple(originals),
                                     report_seal.reference, collection_seal.reference)
    except Exception:
        raise CollectionError('native_collection_failed') from None
    finally:
        if not success and root is not None and root_identity is not None:
            try:
                info = os.lstat(root)
                if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == root_identity:
                    shutil.rmtree(root)
                    _sync(root.parent)
            except OSError:
                pass
