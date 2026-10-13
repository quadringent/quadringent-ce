from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Protocol

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

    def get(self, key: str) -> bytes:
        """Read one previously published object for integrity validation/replay."""

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        """Read at most max_bytes plus one overflow-detection byte."""

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        """Enumerate receipt keys only; reject rather than truncate overflow."""


class CheckpointStore(Protocol):
    """Durable source-offset contract used after raw publication."""

    def load(self) -> JournalPosition | None:
        """Read the current durable position."""

    def load_bound(self) -> BoundCheckpoint | None: ...

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
            if path.read_bytes() != content:
                raise ValueError(f"object collision with different content: {key}")
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
                if path.read_bytes() != content:
                    raise ValueError(f"object collision with different content: {key}")
                _sync_directory(path.parent)
                return False
            _sync_directory(path.parent)
            return True
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

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


def _validate_read_budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('invalid object read budget')


def _read_bounded(handle, max_bytes: int) -> bytes:
    payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError('object exceeds read budget')
    return payload


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
    observe during the idempotence check.
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
            metadata = {str(k).lower(): str(v) for k, v in existing.get("Metadata", {}).items()}
            if metadata.get("sha256") == digest:
                return False
            existing_content = self.client.get_object(Bucket=self.bucket, Key=full_key)["Body"].read()
            if existing_content != content:
                raise ValueError(f"object collision with different content: {full_key}")
            return False

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
            metadata = {str(k).lower(): str(v) for k, v in existing.get("Metadata", {}).items()}
            if metadata.get("sha256") == digest:
                return False
            existing_content = self.client.get_object(Bucket=self.bucket, Key=full_key)["Body"].read()
            if existing_content != content:
                raise ValueError(f"object collision with different content: {full_key}")
            return False

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
