"""Google Cloud Storage backend: raw objects, checkpoints and source gate.

GCS offers the two primitives the capture relies on: create-if-absent
(``if_generation_match=0``) and compare-and-set on an object generation.
Raw batches therefore keep their write-once, hash-checked semantics, and a
checkpoint or source-gate record is one small JSON object replaced only
from the exact generation that was read. ``google-cloud-storage`` is
imported lazily so offline tests and AWS installations do not need it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

from .checkpoint import (
    CheckpointObservation,
    _OBSERVATION_MAX_BYTES,
    _bound_record,
    _checkpoint_position,
    _content_version,
    _decode_checkpoint_json,
    _legacy_record,
    _observed_json,
    _parse_checkpoint_record,
    _validate_bound_update,
)
from .contract import JournalPosition
from .object_store import (
    _ArchiveReader,
    _FILE_ARCHIVE_CHUNK,
    _archive_arguments,
    _archive_fence,
    _archive_snapshot,
    _archive_verify,
    _read_bounded,
    _validate_object_key,
    _validate_read_budget,
)
from .receiver_identity import BoundCheckpoint
from .source_gate import GATE_FORMAT_VERSION, SourceGatePolicy, _RevisionedSourceGate, _empty_record

CHECKPOINT_FORMAT_VERSION = "as400-checkpoint-v1"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


def _client(client: Any | None) -> Any:
    if client is not None:
        return client
    from google.cloud import storage

    return storage.Client()


def _valid_bucket(bucket: str) -> str:
    if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
        raise ValueError("invalid GCS bucket")
    return bucket


def _code(error: Exception) -> int | None:
    code = getattr(error, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_not_found(error: Exception) -> bool:
    return _code(error) == 404


def _is_precondition_failed(error: Exception) -> bool:
    return _code(error) == 412


class GcsObjectStore:
    """GCS implementation of ``ObjectStore`` with hash-checked idempotent writes.

    The runtime identity needs ``storage.objects.get``, ``create`` and
    ``list`` on the bucket (e.g. roles/storage.objectCreator plus
    objectViewer), restricted by IAM condition to the configured prefix
    where the site requires it. No delete or overwrite permission is used.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        self.bucket_name = _valid_bucket(bucket)
        self.prefix = prefix.strip("/")
        self.client = _client(client)
        self._bucket = self.client.bucket(self.bucket_name)

    def put_once(self, key: str, content: bytes) -> bool:
        full_key = self._key(key)
        digest = hashlib.sha256(content).hexdigest()
        existing = self._bucket.get_blob(full_key)
        if existing is not None:
            return self._same_existing(existing, full_key, content, digest)
        blob = self._bucket.blob(full_key)
        blob.metadata = {"sha256": digest}
        try:
            blob.upload_from_string(content, content_type="application/octet-stream",
                                    if_generation_match=0)
            return True
        except Exception as error:
            if not _is_precondition_failed(error):
                raise
        # A concurrent writer won: apply the same content check rather than
        # silently accepting a conflicting artifact.
        existing = self._bucket.get_blob(full_key)
        if existing is None:
            raise RuntimeError(f"object precondition failed but object is absent: {full_key}")
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
            existing = self._bucket.get_blob(full_key, timeout=60, retry=None)
            _archive_fence(check_current)
            if existing is not None:
                self._verify_file_archive(existing, snapshot, expected_sha256, byte_count, check_current)
                return False
            blob = self._bucket.blob(full_key)
            blob.chunk_size = _FILE_ARCHIVE_CHUNK
            blob.metadata = {"sha256": expected_sha256}
            _archive_fence(check_current)
            try:
                # Explicit size bounds the SDK's small multipart buffer; our readback checks SHA-256.
                blob.upload_from_file(
                    _ArchiveReader(snapshot, check_current, 0, byte_count),
                    size=byte_count,
                    rewind=True,
                    if_generation_match=0,
                    checksum=None,
                    timeout=60,
                    retry=None,
                )
                created = True
            except Exception as error:
                if not _is_precondition_failed(error):
                    raise
                _archive_fence(check_current)
                blob = self._bucket.get_blob(full_key, timeout=60, retry=None)
                created = False
                if blob is None:
                    raise ValueError("file archive race winner is unavailable") from error
            _archive_fence(check_current)
            self._verify_file_archive(blob, snapshot, expected_sha256, byte_count, check_current)
            _archive_fence(check_current)
            return created

    def verify_file(
        self, key: str, *, expected_sha256: str, byte_count: int, check_current: Callable[[], bool] = lambda: True
    ) -> None:
        _archive_arguments(expected_sha256, byte_count, check_current)
        _archive_fence(check_current)
        full_key = self._key(_validate_object_key(key))
        blob = self._bucket.get_blob(full_key, timeout=60, retry=None)
        _archive_fence(check_current)
        if blob is None:
            raise FileNotFoundError(key)
        self._verify_file_archive(blob, None, expected_sha256, byte_count, check_current)

    @staticmethod
    def _verify_file_archive(blob, snapshot, expected_sha256, byte_count, check_current):
        generation = getattr(blob, "generation", None)
        if (
            type(generation) not in {str, int}
            or re.fullmatch(r"[1-9][0-9]{0,63}", str(generation)) is None
            or type(blob.size) is not int
            or blob.size != byte_count
        ):
            raise ValueError("file archive object length or generation differs")
        source = _GcsArchiveReader(blob, int(generation), byte_count, check_current)
        _archive_verify(source, snapshot, expected_sha256, byte_count, check_current)
        _archive_fence(check_current)

    def _same_existing(self, blob: Any, full_key: str, content: bytes, digest: str) -> bool:
        size = getattr(blob, "size", None)
        if size is not None and (type(size) is not int or size != len(content)):
            raise ValueError(f"object collision with different size: {full_key}")
        if size == 0:
            # Native metadata proves an empty body; no byte range is needed.
            return False
        metadata = {str(k).lower(): str(v) for k, v in (blob.metadata or {}).items()}
        if metadata.get("sha256") == digest:
            return False
        generation = blob.generation
        if (type(generation) not in {str, int}
                or re.fullmatch(r"[1-9][0-9]{0,63}", str(generation)) is None):
            raise ValueError(f"invalid object collision generation: {full_key}")
        observed = blob.download_as_bytes(start=0, end=len(content), if_generation_match=int(generation))
        if (type(observed) is not bytes or observed != content
                or str(blob.generation) != str(generation)):
            raise ValueError(f"object collision with different content: {full_key}")
        return False

    def get(self, key: str) -> bytes:
        return self._bucket.blob(self._key(key)).download_as_bytes()

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        _validate_read_budget(max_bytes)
        blob = self._bucket.blob(self._key(key))
        try:
            payload = blob.download_as_bytes(start=0, end=max_bytes)
        except Exception as error:
            if _is_not_found(error):
                raise FileNotFoundError(key) from error
            raise
        return _read_bounded(_Buffer(payload), max_bytes)

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        prefix = self._key("receipts") + "/"
        keys: list[str] = []
        for blob in self.client.list_blobs(self.bucket_name, prefix=prefix, max_results=max_keys + 1):
            if not blob.name.startswith(prefix):
                raise ValueError("receipt listing escaped prefix")
            keys.append("receipts/" + blob.name[len(prefix):])
            if len(keys) > max_keys:
                raise ValueError("receipt listing exceeds budget")
        return tuple(keys)

    def list_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        if self.prefix:
            _validate_object_key(self.prefix)
        prefix = self.prefix + '/' if self.prefix else ''
        keys = set()
        token = None
        seen_tokens = set()
        while True:
            args = {'prefix': prefix, 'page_size': min(1000, max_keys - len(keys) + 1)}
            if token is not None:
                args['page_token'] = token
            iterator = self.client.list_blobs(self.bucket_name, **args)
            pages = iter(iterator.pages)
            page = next(pages, None)
            if page is None and iterator.next_page_token is not None:
                raise ValueError('invalid object listing pagination')
            for blob in page or ():
                name = getattr(blob, 'name', None)
                if type(name) is not str or not name.startswith(prefix):
                    raise ValueError('object listing escaped prefix')
                key = _validate_object_key(name[len(prefix):])
                if key in keys:
                    raise ValueError('duplicate object listing key')
                keys.add(key)
                if len(keys) > max_keys:
                    raise ValueError('object listing exceeds budget')
            token = iterator.next_page_token
            if token is None:
                return tuple(sorted(keys))
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


class _GcsArchiveReader:
    def __init__(self, blob, generation, size, check_current):
        self.blob, self.generation, self.size = blob, generation, size
        self.check_current, self.offset = check_current, 0

    def read(self, size):
        _archive_fence(self.check_current)
        if type(size) is not int or not 0 < size <= _FILE_ARCHIVE_CHUNK:
            raise ValueError("file archive range must be bounded")
        if self.offset == self.size:
            # Generation-bound size supplies EOF, including empty objects (no invalid empty range).
            return b""
        count = min(size, self.size - self.offset)
        data = self.blob.download_as_bytes(
            start=self.offset,
            end=self.offset + count - 1,
            if_generation_match=self.generation,
            raw_download=True,
            timeout=60,
            retry=None,
        )
        _archive_fence(self.check_current)
        if (
            type(data) is not bytes
            or len(data) != count
            or str(self.blob.generation) != str(self.generation)
            or self.blob.size != self.size
        ):
            raise ValueError("file archive generation or range response differs")
        self.offset += len(data)
        return data


class _Buffer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class _GenerationRecord:
    """One JSON object replaced only from the generation that was read."""

    def __init__(self, bucket: str, name: str, client: Any | None) -> None:
        self.client = _client(client)
        self._bucket = self.client.bucket(_valid_bucket(bucket))
        self.name = name

    def read(self) -> tuple[dict[str, Any] | None, int]:
        blob = self._bucket.get_blob(self.name)
        if blob is None:
            return None, 0
        try:
            payload = blob.download_as_bytes(if_generation_match=blob.generation)
        except Exception as error:
            if _is_precondition_failed(error) or _is_not_found(error):
                # Replaced or removed between metadata and content reads:
                # report a generation no writer can match, so CAS fails.
                return self._reread()
            raise
        record = _decode_checkpoint_json(payload)
        return record, int(blob.generation)

    def _reread(self) -> tuple[dict[str, Any] | None, int]:
        blob = self._bucket.get_blob(self.name)
        if blob is None:
            return None, 0
        record = _decode_checkpoint_json(blob.download_as_bytes(if_generation_match=blob.generation))
        return record, int(blob.generation)

    def write(self, record: dict[str, Any], expected_generation: int) -> bool:
        blob = self._bucket.blob(self.name)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            blob.upload_from_string(payload, content_type="application/json",
                                    if_generation_match=expected_generation)
            return True
        except Exception as error:
            if _is_precondition_failed(error):
                return False
            raise


def _record_name(root: str, key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("record key must not be empty")
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    readable = re.sub(r"[^a-z0-9-]+", "-", key.strip().lower()).strip("-")[:80] or "record"
    return f"{root}/{readable}-{digest}.json"


class GcsCheckpointStore:
    """``CheckpointStore`` backed by one GCS object per stream.

    Semantics match ``DynamoDbCheckpointStore``: commits never move
    backwards, a receiver change is an explicit transition, and every write
    is conditional on the exact predecessor observed by the caller.
    """

    def __init__(self, bucket: str, stream_key: str, *, client: Any | None = None,
                 root: str = "checkpoints") -> None:
        if not isinstance(stream_key, str) or not stream_key.strip():
            raise ValueError("checkpoint stream key must not be empty")
        self.stream_key = stream_key
        self._record = _GenerationRecord(bucket, _record_name(root.strip("/"), stream_key), client)

    @property
    def object_name(self) -> str:
        return self._record.name

    def _read_checkpoint(self) -> tuple[JournalPosition | BoundCheckpoint | None, int]:
        record, generation = self._record.read()
        if record is None:
            return None, 0
        try:
            if type(record.get("stream_id")) is not str or record["stream_id"] != self.stream_key:
                raise KeyError("stream_id")
            return _parse_checkpoint_record({key: value for key, value in record.items() if key != "stream_id"}), generation
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid GCS checkpoint record") from error

    def load(self) -> JournalPosition | None:
        return _checkpoint_position(self._read_checkpoint()[0])

    def load_bound(self) -> BoundCheckpoint | None:
        return _bound_record(self._read_checkpoint()[0])

    def observe_bound(self) -> CheckpointObservation | None:
        blob = self._record._bucket.get_blob(self._record.name)
        if blob is None:
            return None
        generation = getattr(blob, "generation", None)
        arguments = {"start": 0, "end": _OBSERVATION_MAX_BYTES}
        if generation is not None:
            if (type(generation) not in {str, int}
                    or re.fullmatch(r"[1-9][0-9]{0,63}", str(generation)) is None):
                raise ValueError("invalid checkpoint observation generation")
            generation = int(generation)
            arguments["if_generation_match"] = generation
        try:
            content = blob.download_as_bytes(**arguments)
        except Exception as error:
            if _is_precondition_failed(error) or _is_not_found(error):
                raise ValueError("checkpoint changed during observation") from error
            raise
        if generation is not None and str(blob.generation) != str(generation):
            raise ValueError("checkpoint generation changed during observation")
        position = _observed_json(content, stream_key=self.stream_key)
        version = _content_version(content) if generation is None else f"gcs-generation:{generation}"
        return CheckpointObservation(position, content, "checkpoint-json-v2", version)

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self.compare_and_set(previous, position)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """CAS a receiver change against the exact predecessor position."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self.compare_and_set(previous, position)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError("checkpoint moved backwards")
        current, generation = self._read_checkpoint()
        if isinstance(current, BoundCheckpoint):
            raise ValueError("legacy checkpoint write cannot overwrite bound checkpoint v2")
        if current != previous:
            raise RuntimeError("checkpoint compare-and-set conflict")
        record = {**_legacy_record(position), "stream_id": self.stream_key}
        if not self._record.write(record, generation):
            raise RuntimeError("checkpoint compare-and-set conflict")

    def compare_and_set_bound(self, previous: BoundCheckpoint | None, candidate: BoundCheckpoint) -> None:
        _validate_bound_update(previous, candidate)
        current, generation = self._read_checkpoint()
        if _bound_record(current) != previous:
            raise RuntimeError("checkpoint compare-and-set conflict")
        record = {**candidate.to_dict(), "stream_id": self.stream_key}
        if not self._record.write(record, generation):
            raise RuntimeError("checkpoint compare-and-set conflict")


class GcsSourceGate(_RevisionedSourceGate):
    """Source gate stored as one GCS object; the generation is the revision."""

    def __init__(self, bucket: str, gate_key: str, *, policy: SourceGatePolicy | None = None,
                 client: Any | None = None, root: str = "source-gates") -> None:
        if not isinstance(gate_key, str) or not gate_key.strip() or len(gate_key) > 256:
            raise ValueError("invalid source gate key")
        super().__init__(policy)
        self.gate_key = gate_key
        self._record = _GenerationRecord(bucket, _record_name(root.strip("/"), gate_key), client)

    def _read(self) -> dict[str, Any]:
        stored, generation = self._record.read()
        if stored is None:
            record = _empty_record(datetime.now(timezone.utc))
        else:
            if stored.get("format_version") != GATE_FORMAT_VERSION or stored.get("gate_key") != self.gate_key:
                raise ValueError("source gate record format unknown")
            record = {k: v for k, v in stored.items() if k != "gate_key"}
        record["revision"] = generation
        return record

    def _cas_write(self, record: dict[str, Any], expected_revision: int) -> bool:
        stored = {k: v for k, v in record.items() if k != "revision" and v is not None}
        stored["format_version"] = GATE_FORMAT_VERSION
        stored["gate_key"] = self.gate_key
        return self._record.write(stored, expected_revision)
