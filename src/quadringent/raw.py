from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Sequence

from .contract import ChangeEvent, JournalPosition, deduplicate
from .capture_binding import CaptureBinding
from .receiver_identity import BoundCheckpoint


@dataclass(frozen=True)
class RawBatchManifest:
    batch_id: str
    format_version: str
    event_count: int
    event_ids: tuple[str, ...]
    high_watermark: JournalPosition
    payload_sha256: str
    capture_binding: CaptureBinding | None = None
    bound_high_watermark: BoundCheckpoint | None = None
    copy_context: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        record = {
            "batch_id": self.batch_id,
            "format_version": self.format_version,
            "event_count": self.event_count,
            "event_ids": list(self.event_ids),
            "high_watermark": {
                "receiver": self.high_watermark.receiver,
                "sequence": self.high_watermark.sequence,
            },
            "payload_sha256": self.payload_sha256,
        }
        if self.capture_binding is not None:
            record['capture_binding'] = self.capture_binding.to_dict()
            if self.copy_context is not None:
                record['copy_context'] = self.copy_context
            else:
                record['bound_high_watermark'] = self.bound_high_watermark.to_dict()
        return record


@dataclass(frozen=True)
class RawBatch:
    manifest: RawBatchManifest
    events: tuple[ChangeEvent, ...]


class RawBatchWriter:
    """Append-only local raw writer used by the POC.

    It writes the JSONL payload and its manifest with temp-file + fsync +
    rename. It intentionally does not update a checkpoint. The caller must
    call ``OffsetLedger.commit_raw`` only after this method returns, which
    makes the crash-before-checkpoint behavior testable and explicit.
    """

    FORMAT_VERSION = "as400-raw-v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_batch(
        self,
        events: Iterable[ChangeEvent],
        *,
        high_watermark: JournalPosition,
    ) -> RawBatchManifest:
        unique_events = deduplicate(list(events))
        if not unique_events:
            raise ValueError("cannot write an empty raw batch")
        previous_sequence: int | None = None
        for event in unique_events:
            if event.position.receiver != high_watermark.receiver:
                raise ValueError("raw batch cannot cross receivers implicitly")
            if previous_sequence is not None and event.position.sequence < previous_sequence:
                raise ValueError("raw batch events must be ordered by journal sequence")
            if event.position.sequence > high_watermark.sequence:
                raise ValueError("high watermark must cover every event in the batch")
            previous_sequence = event.position.sequence

        lines = [
            _canonical_json(event.to_record()).encode("utf-8") + b"\n"
            for event in unique_events
        ]
        payload = b"".join(lines)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        batch_id = _batch_id(
            self.FORMAT_VERSION,
            [event.event_id for event in unique_events],
            high_watermark,
            payload_sha256,
        )
        manifest = RawBatchManifest(
            batch_id=batch_id,
            format_version=self.FORMAT_VERSION,
            event_count=len(unique_events),
            event_ids=tuple(event.event_id for event in unique_events),
            high_watermark=high_watermark,
            payload_sha256=payload_sha256,
        )

        payload_path = self.root / f"batch-{batch_id}.jsonl"
        manifest_path = self.root / f"batch-{batch_id}.manifest.json"
        _write_once(payload_path, payload)
        _write_once(manifest_path, _canonical_json(manifest.to_record()).encode("utf-8") + b"\n")
        return manifest


class RawBatchReader:
    """Integrity-checking reader for deterministic raw replay.

    A batch is accepted only when the payload hash, event count, event IDs and
    high-watermark recorded in its manifest all match the JSONL contents. This
    is intentionally a local filesystem adapter for the POC; an S3 adapter can
    reuse the same validation rules without changing the event contract. Batches
    are ordered by watermark, and a multi-receiver replay must provide the
    explicit receiver chain supplied by IBM i metadata.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        receiver_order: Sequence[str] | None = None,
    ) -> None:
        self.root = Path(root)
        if receiver_order is not None:
            normalized = tuple(str(receiver).strip() for receiver in receiver_order)
            if not normalized or any(not receiver for receiver in normalized):
                raise ValueError("receiver order must contain non-empty names")
            if len(set(normalized)) != len(normalized):
                raise ValueError("receiver order contains a duplicate")
            self.receiver_order: tuple[str, ...] | None = normalized
        else:
            self.receiver_order = None

    def read_batches(self) -> list[RawBatch]:
        batches: list[RawBatch] = []
        for manifest_path in sorted(self.root.glob("*.manifest.json")):
            batches.append(self._read_batch(manifest_path))
        if not batches:
            return []
        bindings = [batch.manifest.capture_binding for batch in batches]
        if any(binding is not None for binding in bindings) and any(binding != bindings[0] for binding in bindings):
            raise ValueError('RAW replay cannot mix capture bindings or legacy batches')
        native_names = {}
        for batch in batches:
            point = batch.manifest.bound_high_watermark
            if point is not None:
                identity = point.receiver_identity
                if native_names.setdefault(identity.receiver_name, identity) != identity:
                    raise ValueError('RAW replay reuses a receiver name with another native identity')
        receivers = {batch.manifest.high_watermark.receiver for batch in batches}
        if self.receiver_order is None and len(receivers) != 1:
            raise ValueError("raw replay requires explicit receiver order")
        if self.receiver_order is not None:
            receiver_index = {
                receiver: index for index, receiver in enumerate(self.receiver_order)
            }
            missing = receivers - receiver_index.keys()
            if missing:
                raise ValueError("raw replay receiver is absent from explicit order")
        else:
            receiver_index = {receiver: 0 for receiver in receivers}
        ordered = sorted(
            batches,
            key=lambda batch: (
                receiver_index[batch.manifest.high_watermark.receiver],
                batch.manifest.high_watermark.sequence,
            ),
        )
        for previous, current in zip(ordered, ordered[1:]):
            same_receiver = (
                current.manifest.high_watermark.receiver
                == previous.manifest.high_watermark.receiver
            )
            if same_receiver and (
                current.manifest.high_watermark.sequence
                == previous.manifest.high_watermark.sequence
            ):
                raise ValueError("raw replay contains duplicate high-watermark sequence")
        return ordered

    def replay(self) -> list[ChangeEvent]:
        events: list[ChangeEvent] = []
        for batch in self.read_batches():
            events.extend(batch.events)
        return deduplicate(events)

    def _read_batch(self, manifest_path: Path) -> RawBatch:
        payload_path = manifest_path.with_name(manifest_path.name.removesuffix(".manifest.json") + ".jsonl")
        if not payload_path.exists():
            raise FileNotFoundError(f"missing raw payload: {payload_path}")
        return read_raw_batch(
            manifest_path.read_bytes(),
            payload_path.read_bytes(),
            payload_name=str(payload_path),
        )


def read_raw_batch(
    manifest_content: bytes,
    payload: bytes,
    *,
    payload_name: str = "raw payload",
    preserve_decimals: bool = False,
) -> RawBatch:
    """Validate and reconstruct a batch from bytes returned by any store."""

    manifest_record = json.loads(manifest_content.decode("utf-8"), object_pairs_hook=_unique_fields)
    if type(manifest_record) is not dict:
        raise ValueError('RAW manifest must be an object')
    binding = None
    bound_watermark = None
    copy_context = None
    version = manifest_record.get("format_version")
    if version in ("as400-raw-v2", "as400-snapshot-v2"):
        context_field = 'copy_context' if version == 'as400-snapshot-v2' else 'bound_high_watermark'
        if set(manifest_record) != {"batch_id", "format_version", "event_count", "event_ids",
                                   "high_watermark", "payload_sha256", "capture_binding",
                                   context_field}:
            raise ValueError("bound RAW manifest fields invalid")
        binding = CaptureBinding.from_dict(manifest_record["capture_binding"])
        if (type(manifest_record['event_count']) is not int or manifest_record['event_count'] < 1
                or type(manifest_record['event_ids']) is not list
                or any(type(value) is not str for value in manifest_record['event_ids'])
                or type(manifest_record['high_watermark']) is not dict
                or set(manifest_record['high_watermark']) != {'receiver', 'sequence'}
                or type(manifest_record['high_watermark']['sequence']) is not int
                or type(manifest_record['high_watermark']['receiver']) is not str):
            raise ValueError('bound RAW fields must preserve their exact types')
        if version == 'as400-snapshot-v2':
            copy_context = _validated_copy_context(binding, manifest_record['copy_context'])
        else:
            bound_watermark = BoundCheckpoint.from_dict(manifest_record["bound_high_watermark"])
            binding.validate_checkpoint(bound_watermark)
    elif any(field in manifest_record for field in ('capture_binding', 'bound_high_watermark', 'copy_context')):
        raise ValueError("legacy RAW cannot carry an ignored binding")
    watermark_record = manifest_record["high_watermark"]
    manifest = RawBatchManifest(
        batch_id=str(manifest_record["batch_id"]),
        format_version=str(manifest_record["format_version"]),
        event_count=int(manifest_record["event_count"]),
        event_ids=tuple(str(item) for item in manifest_record["event_ids"]),
        high_watermark=JournalPosition(
            receiver=str(watermark_record["receiver"]),
            sequence=int(watermark_record["sequence"]),
        ),
        payload_sha256=str(manifest_record["payload_sha256"]),
        capture_binding=binding,
        bound_high_watermark=bound_watermark,
        copy_context=copy_context,
    )
    if manifest.format_version not in (RawBatchWriter.FORMAT_VERSION, "as400-raw-v2", 'as400-snapshot-v2'):
        raise ValueError(f"unsupported raw format: {manifest.format_version}")
    if bound_watermark is not None and bound_watermark.position != manifest.high_watermark:
        raise ValueError("bound RAW watermark projection differs")

    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != manifest.payload_sha256:
        raise ValueError(f"raw payload hash mismatch: {payload_name}")

    # Le chemin SQL caste la valeur selon le type IBM i découvert. Pour un
    # DECIMAL issu du lecteur Java, convertir d'abord le littéral JSON en
    # float Python ferait perdre des chiffres avant même le MERGE Snowflake.
    # Les chiffres décimaux restent des chaînes exactes jusqu'au CAST SQL.
    events = tuple(
        ChangeEvent.from_record(json.loads(line, parse_float=str if preserve_decimals else float))
        for line in payload.splitlines()
        if line.strip()
    )
    if len(events) != manifest.event_count:
        raise ValueError(f"raw event count mismatch: {payload_name}")
    if tuple(event.event_id for event in events) != manifest.event_ids:
        raise ValueError(f"raw event identity mismatch: {payload_name}")
    previous_position: JournalPosition | None = None
    for event in events:
        if previous_position is not None:
            if event.position.receiver != previous_position.receiver:
                raise ValueError(f"raw batch cannot cross receivers implicitly: {payload_name}")
            if event.position.sequence < previous_position.sequence:
                raise ValueError(f"raw batch events must be ordered by journal sequence: {payload_name}")
        previous_position = event.position
    expected_batch_id = _batch_id(
        manifest.format_version,
        list(manifest.event_ids),
        manifest.high_watermark,
        manifest.payload_sha256,
        capture_binding=binding, bound_high_watermark=bound_watermark,
        copy_context=copy_context,
    )
    if manifest.batch_id != expected_batch_id:
        raise ValueError(f"raw batch identity mismatch: {payload_name}")
    if any(event.position.receiver != manifest.high_watermark.receiver
           or event.position.sequence > manifest.high_watermark.sequence
           for event in events):
        raise ValueError(f"raw high-watermark mismatch: {payload_name}")
    if binding is not None and copy_context is None and any((event.source_system, event.journal) !=
                                   (binding.source_system, binding.journal) for event in events):
        raise ValueError("RAW events differ from the admitted EVENT_ID domain")
    if copy_context is not None and (manifest.high_watermark.receiver != 'SNAPSHOT:' + copy_context['run_id']
            or not events or any(event.source_system != binding.source_system
                                 or event.journal != f'SNAPSHOT:{event.library}.{event.table}'
                                 or (event.library, event.table) != (events[0].library, events[0].table)
                                 for event in events)):
        raise ValueError('snapshot payload differs from the admitted copy attempt')
    return RawBatch(manifest=manifest, events=events)


def bind_raw_batch(manifest_content: bytes, payload: bytes, *, binding: CaptureBinding,
                   end: BoundCheckpoint) -> bytes:
    """Bind certified source bytes without changing historical EVENT_IDs."""
    binding.validate_checkpoint(end)
    batch = read_raw_batch(manifest_content, payload)
    if batch.manifest.high_watermark != end.position:
        raise ValueError("RAW does not reach the bound scan end")
    if batch.manifest.capture_binding is not None:
        if batch.manifest.capture_binding != binding or batch.manifest.bound_high_watermark != end:
            raise ValueError("RAW already belongs to another binding")
        return manifest_content
    record = batch.manifest.to_record()
    record.update(format_version="as400-raw-v2", capture_binding=binding.to_dict(),
                  bound_high_watermark=end.to_dict())
    record["batch_id"] = _batch_id("as400-raw-v2", list(batch.manifest.event_ids), end.position,
                                    batch.manifest.payload_sha256,
                                    capture_binding=binding, bound_high_watermark=end)
    content = _canonical_json(record).encode() + b"\n"
    read_raw_batch(content, payload)
    return content


def _validated_copy_context(binding: CaptureBinding, context: object) -> dict[str, Any]:
    if type(context) is not dict or set(context) != {'pipeline_id', 'table_id', 'run_id', 'boundary'}:
        raise ValueError('snapshot copy context fields invalid')
    if any(type(context[field]) is not str or not context[field] or context[field].strip() != context[field]
           or any(ord(char) < 32 for char in context[field])
           for field in ('pipeline_id', 'table_id', 'run_id')):
        raise ValueError('snapshot copy context identity invalid')
    binding.validate_checkpoint(BoundCheckpoint.from_dict(context['boundary']))
    return context


def bind_snapshot_batch(manifest_content: bytes, payload: bytes, *, binding: CaptureBinding,
                        boundary: BoundCheckpoint, pipeline_id: str, table_id: str, run_id: str) -> bytes:
    binding.validate_checkpoint(boundary)
    context = _validated_copy_context(binding, dict(pipeline_id=pipeline_id, table_id=table_id,
                                                   run_id=run_id, boundary=boundary.to_dict()))
    batch = read_raw_batch(manifest_content, payload)
    if batch.manifest.high_watermark.receiver != 'SNAPSHOT:' + run_id:
        raise ValueError('initial-copy RAW must carry its virtual snapshot position')
    if batch.manifest.capture_binding is not None:
        if batch.manifest.capture_binding != binding or batch.manifest.copy_context != context:
            raise ValueError('snapshot already belongs to another copy')
        return manifest_content
    record = batch.manifest.to_record()
    record.update(format_version='as400-snapshot-v2', capture_binding=binding.to_dict(), copy_context=context)
    record['batch_id'] = _batch_id('as400-snapshot-v2', list(batch.manifest.event_ids),
                                   batch.manifest.high_watermark, batch.manifest.payload_sha256,
                                   capture_binding=binding, copy_context=context)
    content = _canonical_json(record).encode() + b'\n'
    read_raw_batch(content, payload)
    return content


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        existing = path.read_bytes()
        if existing != content:
            raise FileExistsError(f"raw artifact collision with different content: {path}")
        return

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unique_fields(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError('duplicate RAW manifest field')
        record[key] = value
    return record


def _batch_id(
    format_version: str,
    event_ids: list[str],
    high_watermark: JournalPosition,
    payload_sha256: str,
    *,
    capture_binding: CaptureBinding | None = None,
    bound_high_watermark: BoundCheckpoint | None = None,
    copy_context: dict[str, Any] | None = None,
) -> str:
    record = {
            "format_version": format_version,
            "event_ids": event_ids,
            "high_watermark": {
                "receiver": high_watermark.receiver,
                "sequence": high_watermark.sequence,
            },
            "payload_sha256": payload_sha256,
        }
    if format_version == "as400-raw-v2":
        if capture_binding is None or bound_high_watermark is None:
            raise ValueError("bound RAW identity requires its full capture binding")
        capture_binding.validate_checkpoint(bound_high_watermark)
        record.update(capture_binding=capture_binding.to_dict(),
                      bound_high_watermark=bound_high_watermark.to_dict())
    elif format_version == 'as400-snapshot-v2':
        if capture_binding is None or bound_high_watermark is not None:
            raise ValueError('snapshot requires its copy context, without a native high watermark')
        record.update(capture_binding=capture_binding.to_dict(),
                      copy_context=_validated_copy_context(capture_binding, copy_context))
    elif capture_binding is not None or bound_high_watermark is not None or copy_context is not None:
        raise ValueError("legacy RAW identity cannot ignore a binding")
    identity = _canonical_json(record).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]
