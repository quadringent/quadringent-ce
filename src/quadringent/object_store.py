from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from .checkpoint import CheckpointObservation

from .contract import ChangeEvent, JournalPosition
from .capture_binding import CaptureBinding
from .receiver_identity import BoundCheckpoint
from .raw import RawBatch, RawBatchManifest, RawBatchWriter, read_raw_batch


def receipt_index_key(position: JournalPosition | BoundCheckpoint) -> str:
    record = position.to_dict() if isinstance(position, BoundCheckpoint) else {
        'receiver': position.receiver, 'sequence': position.sequence}
    identity = json.dumps(record,
                          sort_keys=True, separators=(',', ':')).encode()
    return 'scan-index/end-' + hashlib.sha256(identity).hexdigest() + '.json'


def validate_bound_scan(*, start: BoundCheckpoint, end: BoundCheckpoint,
                        previous: BoundCheckpoint | None, capture_binding: CaptureBinding,
                        manifest_content: bytes | None, payload: bytes | None,
                        scan_completed_at: datetime | None) -> RawBatch | None:
    """Validate the entire scan before publishing any table coverage."""
    if type(capture_binding) is not CaptureBinding or any(type(point) is not BoundCheckpoint
                                                        for point in (start, end)):
        raise ValueError('bound scan requires complete positions and binding')
    for point in (start, end, previous):
        if point is not None:
            capture_binding.validate_checkpoint(point)
    if (start.receiver_identity != end.receiver_identity or start.sequence > end.sequence):
        raise ValueError('invalid bound scanned range')
    if previous is None:
        if (start.receiver_identity != capture_binding.bootstrap.receiver_identity
                or start.sequence != capture_binding.bootstrap.sequence + 1):
            raise ValueError('first bound scan must follow the original bootstrap')
        previous = capture_binding.bootstrap
    if previous.receiver_identity.receiver_name == start.receiver_identity.receiver_name and (
            previous.receiver_identity != start.receiver_identity or start.sequence != previous.sequence + 1):
        raise ValueError('bound scan does not follow its exact predecessor')
    if not isinstance(scan_completed_at, datetime) or scan_completed_at.utcoffset() is None:
        raise ValueError('bound scan requires a timezone-aware completion timestamp')
    if (manifest_content is None) != (payload is None):
        raise ValueError('bound RAW pair incomplete')
    if manifest_content is None:
        return None
    batch = read_raw_batch(manifest_content, payload)
    if (batch.manifest.capture_binding != capture_binding or batch.manifest.bound_high_watermark != end):
        raise ValueError('RAW binding differs from the certified scan')
    if any(event.position.receiver != start.receiver_identity.receiver_name
           or not start.sequence <= event.position.sequence <= end.sequence for event in batch.events):
        raise ValueError('raw events escape scanned range')
    return batch


class ObjectStore(Protocol):
    """Minimal immutable-object contract required by the raw publisher."""

    def put_once(self, key: str, content: bytes) -> bool:
        """Store an object once; return False for an identical existing object."""

    def put_file_once(
        self,
        key: str,
        path: os.PathLike[str],
        *,
        expected_sha256: str,
        byte_count: int,
        check_current: Callable[[], bool] = lambda: True,
    ) -> bool:
        """Conditionally archive and fully verify one bounded-memory file snapshot."""

    def verify_file(
        self, key: str, *, expected_sha256: str, byte_count: int,
        check_current: Callable[[], bool] = lambda: True, output=None
    ) -> None:
        """Verify all bytes; optional output is usable only after successful return."""

    def get(self, key: str) -> bytes:
        """Read one previously published object for integrity validation/replay."""

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        """Read at most max_bytes plus one overflow-detection byte."""

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        """Enumerate receipt keys only; reject rather than truncate overflow."""

    def list_keys(self, max_keys: int) -> tuple[str, ...]:
        """Enumerate every canonical relative key; reject incomplete listings."""


class CheckpointStore(Protocol):
    """Durable source-offset contract used after raw publication."""

    def load(self) -> JournalPosition | None:
        """Read the current durable position."""

    def load_bound(self) -> BoundCheckpoint | None: ...

    def observe_bound(self) -> CheckpointObservation | None:
        """Read one bound checkpoint together with its original content and provenance."""

    def compare_and_set_bound(self, previous: BoundCheckpoint | None,
                              position: BoundCheckpoint) -> None: ...

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        """Atomically advance only from the caller's exact predecessor."""

    def commit(self, position: JournalPosition) -> None:
        """Persist a source position after the corresponding raw batch is safe."""

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """Persist an explicitly validated receiver transition."""


class FileObjectStore:
    """Filesystem implementation used by tests and the offline POC."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        _ensure_durable_directory(self.root)

    def put_once(self, key: str, content: bytes) -> bool:
        path = self._path_for(key)
        if path.exists():
            self._require_same_file(path, key, content)
            _sync_directory(path.parent)
            return False
        _ensure_durable_directory(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Atomic create-if-absent: rename/replace could overwrite a
                # concurrent winner after the existence check above.
                os.link(temporary_path, path)
            except FileExistsError:
                self._require_same_file(path, key, content)
                _sync_directory(path.parent)
                return False
            _sync_directory(path.parent)
            return True
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def put_file_once(
        self,
        key: str,
        path: os.PathLike[str],
        *,
        expected_sha256: str,
        byte_count: int,
        check_current: Callable[[], bool] = lambda: True,
    ) -> bool:
        key = _validate_object_key(key)
        with _archive_snapshot(path, expected_sha256, byte_count, check_current) as snapshot:
            with _archive_parent(self.root, key, check_current, create=True) as (directory, name, validate):
                temporary = ".archive-" + secrets.token_hex(16) + ".tmp"
                validate()
                descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with open(descriptor, "w+b", closefd=True) as output:
                        validate()
                        snapshot.seek(0)
                        while True:
                            _archive_fence(check_current)
                            chunk = snapshot.read(_FILE_ARCHIVE_CHUNK)
                            if not chunk:
                                break
                            _archive_write(output, chunk, check_current)
                        output.flush()
                        os.fsync(output.fileno())
                        _archive_fence(check_current)
                        output.seek(0)
                        _archive_verify(output, snapshot, expected_sha256, byte_count, check_current)
                        validate()
                        if _archive_identity(os.fstat(output.fileno())) != _archive_identity(
                            os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                        ):
                            raise ValueError("file archive temporary identity changed")
                        try:
                            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                            created = True
                        except FileExistsError:
                            created = False
                        validate()
                        os.fsync(directory)
                        _archive_verify_local(
                            name, snapshot, expected_sha256, byte_count, check_current, parent_descriptor=directory
                        )
                        validate()
                        return created
                finally:
                    os.unlink(temporary, dir_fd=directory)

    def verify_file(
        self, key: str, *, expected_sha256: str, byte_count: int,
        check_current: Callable[[], bool] = lambda: True, output=None
    ) -> None:
        _archive_arguments(expected_sha256, byte_count, check_current)
        _archive_fence(check_current)
        with _archive_parent(self.root, _validate_object_key(key), check_current, create=False) as (
            directory,
            name,
            validate,
        ):
            _archive_verify_local(name, None, expected_sha256, byte_count, check_current,
                                  parent_descriptor=directory, output=output)
            validate()

    @staticmethod
    def _require_same_file(path: Path, key: str, content: bytes) -> None:
        with path.open("rb") as handle:
            if not _same_collision_content(handle, content):
                raise ValueError(f"object collision with different content: {key}")

    def get(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        _validate_read_budget(max_bytes)
        with self._path_for(key).open('rb') as handle:
            return _read_bounded(handle, max_bytes)

    def _path_for(self, key: str) -> Path:
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("object key must stay below the configured root")
        return self.root / relative

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        keys = []
        for path in (self.root/'receipts').glob('*.json'):
            keys.append(path.relative_to(self.root).as_posix())
            if len(keys) > max_keys:
                raise ValueError('receipt listing exceeds budget')
        return tuple(keys)

    def list_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        keys = set()
        directories = 0
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def visit(descriptor: int, prefix: str) -> None:
            nonlocal directories
            directories += 1
            if directories > max_keys * 8 + 1:
                raise ValueError('object listing directory budget exceeded')
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    key = prefix + entry.name
                    _validate_object_key(key)
                    if entry.is_symlink():
                        raise ValueError('object listing refuses symlinks')
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            child = os.open(entry.name, flags, dir_fd=descriptor)
                        except OSError as error:
                            raise ValueError('object listing directory changed or became a symlink') from error
                        try:
                            visit(child, key + '/')
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        if key in keys:
                            raise ValueError('duplicate object listing key')
                        keys.add(key)
                        if len(keys) > max_keys:
                            raise ValueError('object listing exceeds budget')
                    else:
                        raise ValueError('object listing refuses special files')

        if self.root.is_symlink():
            raise ValueError('object listing refuses a symlink root')
        try:
            descriptor = os.open(self.root, flags)
        except OSError as error:
            raise ValueError('object listing root changed or became a symlink') from error
        try:
            visit(descriptor, '')
        finally:
            os.close(descriptor)
        return tuple(sorted(keys))


def _validate_object_key(key: object) -> str:
    if (type(key) is not str or not key or len(key.encode('utf-8')) > 1024
            or '\\' in key or any(ord(char) < 32 or ord(char) == 127 for char in key)
            or any(part in ('', '.', '..') or part.strip() != part for part in key.split('/'))):
        raise ValueError('object key must be a canonical relative path')
    return key


def _validate_read_budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('invalid object read budget')


def _read_bounded(handle, max_bytes: int) -> bytes:
    payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError('object exceeds read budget')
    return payload


def _same_collision_content(handle, content: bytes) -> bool:
    observed = bytearray()
    while len(observed) <= len(content):
        chunk = handle.read(len(content) + 1 - len(observed))
        if type(chunk) is not bytes:
            raise ValueError("invalid object collision response")
        observed.extend(chunk)
        if not chunk:
            break
    return bytes(observed) == content


_FILE_ARCHIVE_CHUNK = 1024 * 1024
_FILE_ARCHIVE_MAX_READ = 8 * 1024 * 1024
_S3_ARCHIVE_MIN_PART = 5 * 1024 * 1024


def _archive_fence(check_current: Callable[[], bool]) -> None:
    if check_current() is not True:
        raise ValueError("file archive context is no longer current")


def _archive_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextmanager
def _archive_parent(root: Path, key: str, check_current, *, create: bool):
    """Keep a checked directory chain open for descriptor-relative archive operations."""
    root_parts = root.absolute().parts
    if any(part in {".", ".."} for part in root_parts[1:]):
        raise ValueError("invalid file archive root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors, links = [], []

    def validate():
        _archive_fence(check_current)
        for parent, name, child in links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("file archive destination parent changed")
        _archive_fence(check_current)

    try:
        _archive_fence(check_current)
        directory = os.open(root_parts[0], flags)
        descriptors.append(directory)
        components = [(part, False) for part in root_parts[1:]]
        components.extend((part, create) for part in key.split("/")[:-1])
        for name, may_create in components:
            validate()
            try:
                child = os.open(name, flags, dir_fd=directory)
            except FileNotFoundError:
                if not may_create:
                    raise
                validate()
                try:
                    os.mkdir(name, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                validate()
                os.fsync(directory)
                child = os.open(name, flags, dir_fd=directory)
            descriptors.append(child)
            links.append((directory, name, child))
            if create:
                validate()
                os.fsync(directory)
            directory = child
        validate()
        yield directory, key.split("/")[-1], validate
        validate()
    except OSError as error:
        raise ValueError("file archive destination is unavailable or unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _archive_regular(path: os.PathLike[str], *, parent_descriptor=None):
    parts = Path(path).absolute().parts if parent_descriptor is None else (os.fspath(path),)
    if (
        (parent_descriptor is None and len(parts) < 2)
        or any(part in {".", ".."} for part in parts[1:])
        or (parent_descriptor is not None and ("/" in parts[0] or parts[0] in {"", ".", ".."}))
    ):
        raise ValueError("invalid file archive source path")
    directory = descriptor = None
    try:
        directory = (
            os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if parent_descriptor is None
            else os.dup(parent_descriptor)
        )
        for part in parts[1:-1] if parent_descriptor is None else ():
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("file archive requires a regular file")
        with open(descriptor, "rb", buffering=0, closefd=False) as source:
            yield source, before
        after = os.fstat(descriptor)
        current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if _archive_identity(before) != _archive_identity(after) or _archive_identity(after) != _archive_identity(
            current
        ):
            raise ValueError("file archive source changed during observation")
    except OSError as error:
        raise ValueError("file archive source is unavailable or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _archive_write(output, data: bytes, check_current) -> None:
    pending = memoryview(data)
    while pending:
        _archive_fence(check_current)
        written = output.write(pending)
        if type(written) is not int or not 0 < written <= len(pending):
            raise ValueError("file archive snapshot write incomplete")
        pending = pending[written:]
        _archive_fence(check_current)


def _archive_arguments(expected_sha256: str, byte_count: int, check_current) -> None:
    if (
        type(expected_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(byte_count) is not int
        or byte_count < 0
        or not callable(check_current)
    ):
        raise ValueError("invalid file archive identity")


@contextmanager
def _archive_snapshot(path, expected_sha256: str, byte_count: int, check_current):
    _archive_arguments(expected_sha256, byte_count, check_current)
    _archive_fence(check_current)
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        measured, count = hashlib.sha256(), 0
        with _archive_regular(path) as (source, metadata):
            if metadata.st_size != byte_count:
                raise ValueError("file archive source length differs")
            while True:
                _archive_fence(check_current)
                chunk = source.read(min(_FILE_ARCHIVE_CHUNK, byte_count + 1 - count))
                _archive_fence(check_current)
                if not chunk:
                    break
                count += len(chunk)
                if count > byte_count:
                    raise ValueError("file archive source exceeds expected length")
                measured.update(chunk)
                _archive_write(snapshot, chunk, check_current)
        if count != byte_count or measured.hexdigest() != expected_sha256:
            raise ValueError("file archive source hash or length differs")
        _archive_fence(check_current)
        snapshot.flush()
        os.fsync(snapshot.fileno())
        _archive_fence(check_current)
        snapshot.seek(0)
        yield snapshot


class _ArchiveReader:
    """A seekable, fenced view of an immutable anonymous snapshot."""

    def __init__(self, snapshot, check_current, start: int, size: int):
        self.snapshot, self.check_current = snapshot, check_current
        self.start, self.size, self.offset = start, size, 0
        self.seek(0)

    @property
    def closed(self):
        return self.snapshot.closed

    def tell(self):
        return self.offset

    def seek(self, offset, whence=os.SEEK_SET):
        _archive_fence(self.check_current)
        target = (
            offset if whence == os.SEEK_SET else self.offset + offset if whence == os.SEEK_CUR else self.size + offset
        )
        if whence not in {os.SEEK_SET, os.SEEK_CUR, os.SEEK_END} or not 0 <= target <= self.size:
            raise ValueError("file archive reader seek escaped its part")
        self.snapshot.seek(self.start + target)
        self.offset = target
        _archive_fence(self.check_current)
        return target

    def read(self, size=-1):
        _archive_fence(self.check_current)
        if type(size) is not int or size < 0:
            raise ValueError("file archive reader requires a bounded read")
        requested = min(size, self.size - self.offset, _FILE_ARCHIVE_MAX_READ)
        self.snapshot.seek(self.start + self.offset)
        data = self.snapshot.read(requested)
        if len(data) != requested:
            raise ValueError("file archive snapshot was truncated")
        self.offset += len(data)
        _archive_fence(self.check_current)
        return data


def _archive_verify(source, snapshot, expected_sha256, byte_count, check_current, *, output=None):
    _archive_fence(check_current)
    if snapshot is not None:
        snapshot.seek(0)
    measured, count = hashlib.sha256(), 0
    while True:
        _archive_fence(check_current)
        chunk = source.read(min(_FILE_ARCHIVE_CHUNK, byte_count + 1 - count))
        _archive_fence(check_current)
        if type(chunk) is not bytes or len(chunk) > _FILE_ARCHIVE_CHUNK:
            raise ValueError("invalid file archive readback")
        if not chunk:
            break
        count += len(chunk)
        if count > byte_count or (snapshot is not None and snapshot.read(len(chunk)) != chunk):
            raise ValueError("file archive readback differs")
        measured.update(chunk)
        if output is not None and (type(written := output.write(chunk)) is not int or written != len(chunk)):
            raise ValueError("file archive output incomplete")
    if count != byte_count or measured.hexdigest() != expected_sha256:
        raise ValueError("file archive readback hash or length differs")
    _archive_fence(check_current)


def _archive_verify_local(path, snapshot, expected_sha256, byte_count, check_current, *,
                          parent_descriptor=None, output=None):
    with _archive_regular(path, parent_descriptor=parent_descriptor) as (source, metadata):
        if metadata.st_size != byte_count:
            raise ValueError("file archive collision has a different length")
        _archive_verify(source, snapshot, expected_sha256, byte_count, check_current, output=output)
    _archive_fence(check_current)


def _ensure_durable_directory(path: Path) -> None:
    """Sync each parent entry, even if a concurrent creator made it visible."""
    if path == path.parent:
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        return
    _ensure_durable_directory(path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        if not path.is_dir():
            raise
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class S3ObjectStore:
    """S3 implementation with hash-checked idempotent writes.

    ``boto3`` is imported lazily so the contract and offline tests remain
    dependency-free. The runtime role needs GetObject/PutObject for the
    configured prefix and ListBucket on that prefix: S3 uses the latter to
    distinguish a missing object from an object the caller is not allowed to
    observe during the idempotence check. File archives also need
    AbortMultipartUpload to clean up incomplete multipart uploads.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        if not bucket.strip() or bucket.startswith("-"):
            raise ValueError("invalid S3 bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def put_once(self, key: str, content: bytes) -> bool:
        full_key = self._key(key)
        digest = hashlib.sha256(content).hexdigest()
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=full_key)
        except Exception as error:
            if not _is_not_found(error):
                raise
        else:
            return self._same_existing(existing, full_key, content, digest)

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=full_key,
                Body=content,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
            return True
        except Exception as error:
            if not _is_precondition_failed(error):
                raise
            # A concurrent writer won. Re-read and apply the same content
            # check rather than silently accepting a conflicting artifact.
            existing = self.client.head_object(Bucket=self.bucket, Key=full_key)
            return self._same_existing(existing, full_key, content, digest)

    def put_file_once(
        self,
        key: str,
        path: os.PathLike[str],
        *,
        expected_sha256: str,
        byte_count: int,
        check_current: Callable[[], bool] = lambda: True,
    ) -> bool:
        full_key = self._key(_validate_object_key(key))
        with _archive_snapshot(path, expected_sha256, byte_count, check_current) as snapshot:
            _archive_fence(check_current)
            try:
                existing = self.client.head_object(Bucket=self.bucket, Key=full_key)
            except Exception as error:
                if not _is_not_found(error):
                    raise
            else:
                _archive_fence(check_current)
                self._verify_file_archive(full_key, existing, snapshot, expected_sha256, byte_count, check_current)
                return False
            _archive_fence(check_current)
            try:
                if byte_count <= _S3_ARCHIVE_MIN_PART:
                    response = self.client.put_object(
                        Bucket=self.bucket,
                        Key=full_key,
                        Body=_ArchiveReader(snapshot, check_current, 0, byte_count),
                        ContentLength=byte_count,
                        Metadata={"sha256": expected_sha256},
                        IfNoneMatch="*",
                    )
                else:
                    response = self._multipart_file_archive(full_key, snapshot, expected_sha256, byte_count, check_current)
                created = True
            except Exception as error:
                if not _is_precondition_failed(error):
                    raise
                _archive_fence(check_current)
                response = self.client.head_object(Bucket=self.bucket, Key=full_key)
                created = False
            _archive_fence(check_current)
            self._verify_file_archive(full_key, response, snapshot, expected_sha256, byte_count, check_current)
            _archive_fence(check_current)
            return created

    def _multipart_file_archive(self, key, snapshot, expected_sha256, byte_count, check_current):
        _archive_fence(check_current)
        initiated = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, Metadata={"sha256": expected_sha256})
        upload_id = initiated.get("UploadId")
        if type(upload_id) is not str or not upload_id:
            raise ValueError("multipart upload identity unavailable")
        completed = False
        try:
            _archive_fence(check_current)
            part_size = max(_S3_ARCHIVE_MIN_PART, (byte_count + 9999) // 10000)
            parts = []
            for index, offset in enumerate(range(0, byte_count, part_size), 1):
                _archive_fence(check_current)
                length = min(part_size, byte_count - offset)
                response = self.client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=index,
                    Body=_ArchiveReader(snapshot, check_current, offset, length),
                    ContentLength=length,
                )
                _archive_fence(check_current)
                tag = response.get("ETag")
                if type(tag) is not str or not tag:
                    raise ValueError("multipart part identity unavailable")
                parts.append({"PartNumber": index, "ETag": tag})
            _archive_fence(check_current)
            response = self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}, IfNoneMatch="*"
            )
            if type(response) is not dict or not response.get("ETag"):
                raise ValueError("multipart completion identity unavailable")
            completed = True
            return response
        finally:
            if not completed:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    def verify_file(
        self, key: str, *, expected_sha256: str, byte_count: int,
        check_current: Callable[[], bool] = lambda: True, output=None
    ) -> None:
        _archive_arguments(expected_sha256, byte_count, check_current)
        _archive_fence(check_current)
        full_key = self._key(_validate_object_key(key))
        try:
            identity = self.client.head_object(Bucket=self.bucket, Key=full_key)
        except Exception as error:
            if _is_not_found(error):
                raise FileNotFoundError(key) from error
            raise
        _archive_fence(check_current)
        self._verify_file_archive(full_key, identity, None, expected_sha256, byte_count, check_current, output=output)

    def _verify_file_archive(self, key, identity, snapshot, expected_sha256, byte_count, check_current, *, output=None):
        _archive_fence(check_current)
        tag = identity.get("ETag")
        if type(tag) is not str or not tag:
            raise ValueError("file archive object identity unavailable")
        arguments = {"Bucket": self.bucket, "Key": key, "IfMatch": tag}
        if identity.get("VersionId") is not None:
            if type(identity["VersionId"]) is not str or not identity["VersionId"]:
                raise ValueError("file archive object version invalid")
            arguments["VersionId"] = identity["VersionId"]
        response = self.client.get_object(**arguments)
        body = response["Body"]
        try:
            _archive_fence(check_current)
            if (
                type(response.get("ContentLength")) is not int
                or response["ContentLength"] != byte_count
                or response.get("ETag") != tag
                or ("VersionId" in arguments and response.get("VersionId") != arguments["VersionId"])
            ):
                raise ValueError("file archive object length or identity differs")
            _archive_verify(body, snapshot, expected_sha256, byte_count, check_current, output=output)
        finally:
            body.close()
        _archive_fence(check_current)

    def _same_existing(self, existing: dict[str, Any], full_key: str, content: bytes, digest: str) -> bool:
        self._require_size(existing.get("ContentLength"), content, full_key)
        metadata = {str(k).lower(): str(v) for k, v in existing.get("Metadata", {}).items()}
        if metadata.get("sha256") == digest:
            return False
        response = self.client.get_object(Bucket=self.bucket, Key=full_key)
        body = response["Body"]
        try:
            self._require_size(response.get("ContentLength"), content, full_key)
            if not _same_collision_content(body, content):
                raise ValueError(f"object collision with different content: {full_key}")
        finally:
            body.close()
        return False

    @staticmethod
    def _require_size(size: object, content: bytes, full_key: str) -> None:
        if size is not None and (type(size) is not int or size != len(content)):
            raise ValueError(f"object collision with different size: {full_key}")

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        _validate_read_budget(max_bytes)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as error:
            if _is_not_found(error):
                raise FileNotFoundError(key) from error
            raise
        body = response['Body']
        try:
            return _read_bounded(body, max_bytes)
        finally:
            body.close()

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        prefix = self._key('receipts') + '/'
        keys = []
        token = None
        seen_tokens = set()
        while True:
            args = {'Bucket': self.bucket, 'Prefix': prefix, 'MaxKeys': min(1000, max_keys-len(keys)+1)}
            if token is not None:
                args['ContinuationToken'] = token
            page = self.client.list_objects_v2(**args)
            for item in page.get('Contents', []):
                key = item['Key']
                if not key.startswith(prefix):
                    raise ValueError('receipt listing escaped prefix')
                keys.append('receipts/'+key[len(prefix):])
                if len(keys) > max_keys:
                    raise ValueError('receipt listing exceeds budget')
            if not page.get('IsTruncated', False):
                return tuple(keys)
            token = page.get('NextContinuationToken')
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise ValueError('invalid receipt listing pagination')
            seen_tokens.add(token)
            if len(seen_tokens) > max_keys:
                raise ValueError('receipt listing page budget exceeded')

    def list_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        if self.prefix:
            _validate_object_key(self.prefix)
        prefix = self.prefix + '/' if self.prefix else ''
        keys = set()
        token = None
        seen_tokens = set()
        while True:
            args = {'Bucket': self.bucket, 'Prefix': prefix, 'MaxKeys': min(1000, max_keys - len(keys) + 1)}
            if token is not None:
                args['ContinuationToken'] = token
            page = self.client.list_objects_v2(**args)
            if type(page) is not dict or type(page.get('IsTruncated')) is not bool:
                raise ValueError('invalid object listing pagination')
            contents = page.get('Contents', [])
            if type(contents) is not list or ('KeyCount' in page and (
                    type(page['KeyCount']) is not int or page['KeyCount'] != len(contents))):
                raise ValueError('invalid object listing page')
            for item in contents:
                if type(item) is not dict or type(item.get('Key')) is not str or not item['Key'].startswith(prefix):
                    raise ValueError('object listing escaped prefix')
                key = _validate_object_key(item['Key'][len(prefix):])
                if key in keys:
                    raise ValueError('duplicate object listing key')
                keys.add(key)
                if len(keys) > max_keys:
                    raise ValueError('object listing exceeds budget')
            if not page['IsTruncated']:
                if page.get('NextContinuationToken') is not None:
                    raise ValueError('invalid object listing pagination')
                return tuple(sorted(keys))
            token = page.get('NextContinuationToken')
            if type(token) is not str or not token or token in seen_tokens:
                raise ValueError('invalid object listing pagination')
            seen_tokens.add(token)
            if len(seen_tokens) > max_keys:
                raise ValueError('object listing page budget exceeded')

    def _key(self, key: str) -> str:
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("object key must not escape the configured prefix")
        relative_key = str(relative).replace("\\", "/")
        return f"{self.prefix}/{relative_key}" if self.prefix else relative_key


@dataclass(frozen=True)
class ReceiptCaptureResult:
    receipt: dict[str, object]
    publish_ms: float
    checkpoint_ms: float
    payload_bytes: int
    manifest_bytes: int


@dataclass(frozen=True)
class ObjectPublishResult:
    manifest: RawBatchManifest
    payload_key: str
    manifest_key: str
    payload_created: bool
    manifest_created: bool


@dataclass(frozen=True)
class CaptureResult:
    publish: ObjectPublishResult
    checkpoint_committed: bool
    publish_ms: float = 0.0
    checkpoint_ms: float = 0.0
    payload_bytes: int = 0
    manifest_bytes: int = 0


class RawFirstCaptureCoordinator:
    """Coordinate one raw batch and its source checkpoint.

    The checkpoint call is deliberately after ``publish_raw_batch``. If either
    raw object or its manifest cannot be made durable, the source offset is not
    advanced. If the process stops between publication and checkpoint, a new
    worker can safely publish the same deterministic batch again and then
    commit the same position.
    """

    def __init__(self, store: ObjectStore, checkpoint_store: CheckpointStore) -> None:
        self.store = store
        self.checkpoint_store = checkpoint_store

    def capture(
        self,
        events: list[ChangeEvent],
        *,
        high_watermark: JournalPosition,
    ) -> CaptureResult:
        publish_started = time.perf_counter()
        published = publish_raw_batch(
            self.store,
            events,
            high_watermark=high_watermark,
        )
        publish_ms = (time.perf_counter() - publish_started) * 1000
        checkpoint_started = time.perf_counter()
        self.checkpoint_store.commit(high_watermark)
        checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
        return CaptureResult(
            publish=published,
            checkpoint_committed=True,
            publish_ms=publish_ms,
            checkpoint_ms=checkpoint_ms,
        )

    def capture_raw(
        self,
        manifest_content: bytes,
        payload: bytes,
        *,
        payload_key: str,
        manifest_key: str,
    ) -> CaptureResult:
        """Publish an already encoded batch, then commit its watermark.

        The Java iJournal reader owns the original JSON encoding. Rebuilding
        its events in Python could change decimal formatting or another IBM i
        representation before hashing. This path therefore validates and
        publishes the exact bytes produced by the capture runtime.
        """

        publish_started = time.perf_counter()
        published = publish_raw_artifacts(
            self.store,
            manifest_content,
            payload,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )
        publish_ms = (time.perf_counter() - publish_started) * 1000
        checkpoint_started = time.perf_counter()
        self.checkpoint_store.commit(published.manifest.high_watermark)
        checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
        return CaptureResult(
            publish=published,
            checkpoint_committed=True,
            publish_ms=publish_ms,
            checkpoint_ms=checkpoint_ms,
            payload_bytes=len(payload),
            manifest_bytes=len(manifest_content),
        )

    def capture_raw_transition(
        self,
        manifest_content: bytes,
        payload: bytes,
        *,
        previous: JournalPosition | None,
    ) -> CaptureResult:
        """Publish a batch, then commit an explicitly validated receiver change."""

        if previous is None:
            raise ValueError("receiver transition requires an exact predecessor")
        batch = read_raw_batch(manifest_content, payload)
        publish_started = time.perf_counter()
        published = publish_raw_artifacts(
            self.store,
            manifest_content,
            payload,
            payload_key=f"batch-{batch.manifest.batch_id}.jsonl",
            manifest_key=f"batch-{batch.manifest.batch_id}.manifest.json",
        )
        publish_ms = (time.perf_counter() - publish_started) * 1000
        checkpoint_started = time.perf_counter()
        self.checkpoint_store.transition(previous, published.manifest.high_watermark)
        checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
        return CaptureResult(
            publish=published,
            checkpoint_committed=True,
            publish_ms=publish_ms,
            checkpoint_ms=checkpoint_ms,
            payload_bytes=len(payload),
            manifest_bytes=len(manifest_content),
        )

    def advance_without_raw(
        self,
        position: JournalPosition,
        *,
        previous: JournalPosition | None = None,
    ) -> None:
        """Advance a scanned cursor when a bounded window had no matching row."""

        if previous is None:
            self.checkpoint_store.commit(position)
        else:
            self.checkpoint_store.transition(previous, position)

    def capture_receipted_window(
        self, *, start: JournalPosition, end: JournalPosition,
        previous: JournalPosition | None,
        manifest_content: bytes | None = None, payload: bytes | None = None,
        scan_completed_at: datetime | None = None,
    ) -> dict[str, object]:
        return self.capture_receipted_window_result(
            start=start, end=end, previous=previous,
            manifest_content=manifest_content, payload=payload,
            scan_completed_at=scan_completed_at,
        ).receipt

    def capture_receipted_window_result(
        self, *, start: JournalPosition | BoundCheckpoint, end: JournalPosition | BoundCheckpoint,
        previous: JournalPosition | BoundCheckpoint | None,
        manifest_content: bytes | None = None, payload: bytes | None = None,
        scan_completed_at: datetime | None = None,
        capture_binding: CaptureBinding | None = None,
    ) -> ReceiptCaptureResult:
        """Persist an exact scanned range before its checkpoint, including empty scans.

        Opt-in building block for continuous proof windows; legacy publishers
        remain unchanged. Receiver transitions must come from the ordered source
        planner, never from lexical receiver comparison.
        """
        bound = capture_binding is not None
        if bound:
            validate_bound_scan(start=start, end=end, previous=previous, capture_binding=capture_binding,
                                manifest_content=manifest_content, payload=payload,
                                scan_completed_at=scan_completed_at)
            if not isinstance(start, BoundCheckpoint) or not isinstance(end, BoundCheckpoint):
                raise ValueError('bound receipt requires complete positions')
            for point in (start, end, previous):
                if point is not None:
                    capture_binding.validate_checkpoint(point)
            if start.receiver_identity != end.receiver_identity:
                raise ValueError('a scan cannot cross native receiver identities')
            if scan_completed_at is None:
                raise ValueError('bound receipt requires the completed scan timestamp')
        elif any(isinstance(point, BoundCheckpoint) for point in (start, end, previous)):
            raise ValueError('bound positions require a capture binding')
        start_position = start.position if bound else start
        end_position = end.position if bound else end
        previous_position = previous.position if bound and previous is not None else previous
        if start_position.receiver != end_position.receiver or start.sequence > end.sequence:
            raise ValueError('invalid scanned range')
        if previous is not None and previous_position.receiver == start_position.receiver and start.sequence != previous.sequence + 1:
            raise ValueError('scan does not follow predecessor')
        if bound and previous is not None and previous_position.receiver == start_position.receiver and (
                previous.receiver_identity != start.receiver_identity):
            raise ValueError('same-name scan changed its native receiver identity')
        if bound and previous is None and (start.receiver_identity != capture_binding.bootstrap.receiver_identity
                                          or start.sequence != capture_binding.bootstrap.sequence + 1):
            raise ValueError('first bound scan must follow the original bootstrap')
        current = self.checkpoint_store.load_bound() if bound else self.checkpoint_store.load()
        if current not in (previous, end):
            raise ValueError('checkpoint differs from scanned predecessor')
        if (manifest_content is None) != (payload is None):
            raise ValueError('raw payload and manifest must be supplied together')
        raw = None
        event_count = 0
        if manifest_content is not None:
            batch = read_raw_batch(manifest_content, payload)
            if bound and (batch.manifest.capture_binding != capture_binding
                          or batch.manifest.bound_high_watermark != end):
                raise ValueError('RAW binding differs from the certified scan')
            if batch.manifest.high_watermark != end_position or any(
                e.position.receiver != start_position.receiver or not start.sequence <= e.position.sequence <= end.sequence
                for e in batch.events
            ):
                raise ValueError('raw events escape scanned range')
            event_count = len(batch.events)
            raw = {
                'payload_key': f'batch-{batch.manifest.batch_id}.jsonl',
                'manifest_key': f'batch-{batch.manifest.batch_id}.manifest.json',
                'payload_sha256': hashlib.sha256(payload).hexdigest(),
                'manifest_sha256': hashlib.sha256(manifest_content).hexdigest(),
            }
        def position(value):
            return (None if value is None else value.to_dict() if bound else
                    {'receiver': value.receiver, 'sequence': value.sequence})
        receipt = {'format_version': 'quadringent-scan-receipt-v1', 'previous': position(previous),
                   'start': position(start), 'end': position(end), 'event_count': event_count, 'raw': raw}
        if bound:
            receipt.update(format_version='quadringent-scan-receipt-v3',
                           capture_binding=capture_binding.to_dict())
        # Same range start must never accept a different ending or population.
        identity = json.dumps(position(start), sort_keys=True, separators=(',', ':')).encode()
        key = 'receipts/scan-' + hashlib.sha256(identity).hexdigest() + '.json'
        if scan_completed_at is not None:
            if not isinstance(scan_completed_at, datetime) or scan_completed_at.utcoffset() is None:
                raise ValueError('scan completion requires a timezone-aware datetime')
            timestamp = scan_completed_at.isoformat()
            try:
                existing = json.loads(self.store.get_bounded(key, 16384))
            except Exception as error:
                if not isinstance(error, FileNotFoundError) and not _is_not_found(error):
                    raise
            else:
                if existing.get('format_version') != ('quadringent-scan-receipt-v3' if bound else 'quadringent-scan-receipt-v2'):
                    raise ValueError('cannot retrofit scan time onto legacy receipt')
                timestamp = existing.get('scan_completed_at')
                if not isinstance(timestamp, str) or datetime.fromisoformat(timestamp).utcoffset() is None:
                    raise ValueError('invalid prepared scan timestamp')
            receipt['format_version'] = 'quadringent-scan-receipt-v3' if bound else 'quadringent-scan-receipt-v2'
            receipt['scan_completed_at'] = timestamp
        content = json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        index_key = receipt_index_key(end)
        index_content = json.dumps({'format_version': 'quadringent-scan-index-v2' if bound else 'quadringent-scan-index-v1',
                                    'receipt_key': key, 'receipt_sha256': hashlib.sha256(content).hexdigest()},
                                   sort_keys=True, separators=(',', ':')).encode() + b'\n'
        if current == end:
            # Never retroactively manufacture evidence for an advanced checkpoint.
            if self.store.get(key) != content:
                raise ValueError('committed scan receipt differs')
            if self.store.get(index_key) != index_content:
                raise ValueError('committed scan index differs')
            if raw is not None and (
                self.store.get(raw['payload_key']) != payload or self.store.get(raw['manifest_key']) != manifest_content
            ):
                raise ValueError('committed raw differs')
            return ReceiptCaptureResult(receipt, 0.0, 0.0, 0, 0)
        publish_started = time.perf_counter()
        if raw is not None:
            publish_raw_artifacts(self.store, manifest_content, payload,
                                  payload_key=raw['payload_key'], manifest_key=raw['manifest_key'])
        self.store.put_once(key, content)
        self.store.put_once(index_key, index_content)
        publish_ms = (time.perf_counter() - publish_started) * 1000
        checkpoint_started = time.perf_counter()
        if bound:
            self.checkpoint_store.compare_and_set_bound(previous, end)
        else:
            self.checkpoint_store.compare_and_set(previous, end)
        checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
        return ReceiptCaptureResult(receipt, publish_ms, checkpoint_ms,
                                    len(payload or b''), len(manifest_content or b''))


def read_published_batch(
    store: ObjectStore,
    payload_key: str,
    manifest_key: str,
    *,
    preserve_decimals: bool = False,
) -> RawBatch:
    """Validate and replay one batch after reading it from an object store."""

    manifest_content = store.get(manifest_key)
    payload = store.get(payload_key)
    batch = read_raw_batch(
        manifest_content, payload, payload_name=payload_key, preserve_decimals=preserve_decimals
    )
    expected_payload_key = f"batch-{batch.manifest.batch_id}.jsonl"
    expected_manifest_key = f"batch-{batch.manifest.batch_id}.manifest.json"
    if payload_key != expected_payload_key or manifest_key != expected_manifest_key:
        raise ValueError("raw object keys do not match manifest batch identity")
    return batch


def publish_raw_batch(
    store: ObjectStore,
    events: list[ChangeEvent],
    *,
    high_watermark: JournalPosition,
) -> ObjectPublishResult:
    """Stage one raw batch locally, then publish payload before manifest.

    The caller must commit its source checkpoint only after this function
    returns. The manifest is deliberately the last object, so a consumer never
    discovers a batch before its payload has been stored.
    """

    with tempfile.TemporaryDirectory(prefix="as400-raw-stage-") as staging:
        manifest = RawBatchWriter(staging).write_batch(events, high_watermark=high_watermark)
        payload_key = f"batch-{manifest.batch_id}.jsonl"
        manifest_key = f"batch-{manifest.batch_id}.manifest.json"
        payload_content = (Path(staging) / payload_key).read_bytes()
        manifest_content = (Path(staging) / manifest_key).read_bytes()
        return publish_raw_artifacts(
            store,
            manifest_content,
            payload_content,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )


def publish_raw_artifacts(
    store: ObjectStore,
    manifest_content: bytes,
    payload: bytes,
    *,
    payload_key: str,
    manifest_key: str,
) -> ObjectPublishResult:
    """Publish exact raw bytes after validating their batch contract."""

    batch = read_raw_batch(manifest_content, payload, payload_name=payload_key)
    expected_payload_key = f"batch-{batch.manifest.batch_id}.jsonl"
    expected_manifest_key = f"batch-{batch.manifest.batch_id}.manifest.json"
    if payload_key != expected_payload_key or manifest_key != expected_manifest_key:
        raise ValueError("raw object keys do not match manifest batch identity")

    # The manifest is deliberately published last: its presence is the
    # discovery signal that the payload is already durable.
    payload_created = store.put_once(payload_key, payload)
    manifest_created = store.put_once(manifest_key, manifest_content)
    return ObjectPublishResult(
        manifest=batch.manifest,
        payload_key=payload_key,
        manifest_key=manifest_key,
        payload_created=payload_created,
        manifest_created=manifest_created,
    )


def _is_not_found(error: Exception) -> bool:
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failed(error: Exception) -> bool:
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"412", "PreconditionFailed"}
