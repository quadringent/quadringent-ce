"""Private, operator-attested recovery cuts for explicitly selected replay material."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import copy_intent_key

from .authority_backup import ReceiverAuthorityBackup
from .capture_binding import CaptureBinding, _AUTHORITY
from .checkpoint import _decode_checkpoint_json
from .object_store import _validate_object_key
from .proof_windows import validate_bound_receipt
from .raw import read_raw_batch
from .copy_observation import (
    MANIFEST_VERSION, ObservationReference, read_copy_observations, validate_inventory_originals,
)
from .receiver_identity import BoundCheckpoint
from .receiver_registry import ReceiverNameRegistry


BUNDLE_FORMAT_VERSION = "quadringent-native-recovery-bundle-v1"
_SHA = re.compile(r"[a-f0-9]{64}")
_LIMITATIONS = (
    "Writer suspension is operator-attested; this command does not freeze writers.",
    "Only explicitly selected RAW prefixes and checkpoint keys are included; other replay scopes are excluded.",
    "PostgreSQL, secrets, fleet PVCs and Snowflake data require separate backups and recovery procedures.",
    "A stable local cut does not qualify IBM i recovery or authorize resuming writers.",
)


def _encode(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest(value: object) -> None:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("recovery SHA256 must be lowercase hexadecimal")


def _suspended(value: object) -> None:
    if value is not True:
        raise ValueError("all affected writers must be explicitly suspended")


@dataclass(frozen=True)
class RecoveryBudgets:
    max_keys: int = 10000
    max_object_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_authority_bytes: int = 16 * 1024 * 1024
    max_raw_prefixes: int = 64
    max_checkpoints: int = 256

    def __post_init__(self) -> None:
        ceilings = (10000, 32 * 1024 * 1024, 512 * 1024 * 1024,
                    4 * 1024 * 1024, 16 * 1024 * 1024, 64, 256)
        for name, ceiling in zip(self.__dataclass_fields__, ceilings):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("recovery budget is invalid or exceeds the hard limit")


@dataclass(frozen=True)
class RecoverySelection:
    authority: str
    raw_prefixes: tuple[str, ...]
    checkpoint_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.authority) is not str or _AUTHORITY.fullmatch(self.authority) is None:
            raise ValueError("recovery authority must use the canonical stable namespace")
        for values in (self.raw_prefixes, self.checkpoint_keys):
            if type(values) is not tuple or not values:
                raise ValueError("recovery selection must contain nonempty tuples")
            for value in values:
                _validate_object_key(value)
            if len(set(values)) != len(values):
                raise ValueError("duplicate recovery selection label")
        prefixes = ("receiver-authority-v1",) + self.raw_prefixes
        for index, prefix in enumerate(prefixes):
            for other in prefixes[index + 1:]:
                if prefix == other or prefix.startswith(other + "/") or other.startswith(prefix + "/"):
                    raise ValueError("recovery RAW prefixes and authority must be disjoint")

    def to_dict(self) -> dict[str, Any]:
        return {"authority": self.authority, "raw_prefixes": list(self.raw_prefixes),
                "checkpoint_keys": list(self.checkpoint_keys)}

    @classmethod
    def from_dict(cls, value: object) -> RecoverySelection:
        if (type(value) is not dict or set(value) != {"authority", "raw_prefixes", "checkpoint_keys"}
                or type(value["raw_prefixes"]) is not list or type(value["checkpoint_keys"]) is not list):
            raise ValueError("invalid recovery selection")
        return cls(value["authority"], tuple(value["raw_prefixes"]), tuple(value["checkpoint_keys"]))


class _MemoryObjects:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        try:
            content = self.objects[key]
        except KeyError:
            raise FileNotFoundError("recovery referenced object is absent") from None
        if len(content) > max_bytes:
            raise ValueError("recovery object exceeds read budget")
        return content


def _selection(selection: RecoverySelection, budgets: RecoveryBudgets, storage=None) -> None:
    if type(selection) is not RecoverySelection or type(budgets) is not RecoveryBudgets:
        raise ValueError("invalid recovery selection or budgets")
    if len(selection.raw_prefixes) > budgets.max_raw_prefixes or len(selection.checkpoint_keys) > budgets.max_checkpoints:
        raise ValueError("recovery selection exceeds budget")
    if storage is not None:
        backend = {"aws": "s3", "gcs": "gcs"}.get(storage.kind)
        if backend is None or selection.authority != f"{backend}://{storage.raw_bucket}/receiver-authority-v1":
            raise ValueError("recovery target authority differs from the original")


def _keys(store, max_keys: int) -> tuple[str, ...]:
    keys = store.list_keys(max_keys)
    if type(keys) is not tuple or len(keys) > max_keys:
        raise ValueError("invalid or oversized recovery inventory")
    for key in keys:
        _validate_object_key(key)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("recovery inventory must be exhaustive, unique and sorted")
    return keys


def _read_objects(store, keys: tuple[str, ...], budgets: RecoveryBudgets, max_bytes: int) -> dict[str, bytes]:
    objects = {}
    size = 0
    for key in keys:
        content = store.get_bounded(key, budgets.max_object_bytes)
        if type(content) is not bytes or len(content) > budgets.max_object_bytes:
            raise ValueError("recovery object exceeds byte budget")
        size += len(content)
        if size > max_bytes:
            raise ValueError("recovery inventory exceeds total byte budget")
        objects[key] = content
    return objects


def _stable_objects(store, budgets: RecoveryBudgets, *, max_keys: int | None = None,
                    max_bytes: int | None = None) -> dict[str, bytes]:
    max_keys = budgets.max_keys if max_keys is None else max_keys
    max_bytes = budgets.max_total_bytes if max_bytes is None else max_bytes
    keys = _keys(store, max(1, max_keys))
    if len(keys) > max_keys:
        raise ValueError("recovery inventory exceeds total key budget")
    objects = _read_objects(store, keys, budgets, max_bytes)
    if (_keys(store, max(1, max_keys)) != keys or _read_objects(store, keys, budgets, max_bytes) != objects
            or _keys(store, max(1, max_keys)) != keys):
        raise ValueError("recovery inventory changed during observation")
    return objects


def _registry(binding: CaptureBinding, authority_objects: dict[str, bytes]) -> ReceiverNameRegistry:
    return ReceiverNameRegistry(_MemoryObjects(authority_objects), authority=binding.registry_authority,
                                source_system=binding.source_system, journal=binding.journal)


def _binding_for(point: BoundCheckpoint, authority_objects: dict[str, bytes]) -> CaptureBinding:
    candidates = []
    for content in authority_objects.values():
        record = _decode_checkpoint_json(content)
        if record.get("format_version") == CaptureBinding.FORMAT_VERSION:
            binding = CaptureBinding.from_dict(record)
            if binding.scope_id == point.scope_id:
                candidates.append(binding)
    if len(candidates) != 1:
        raise ValueError("checkpoint original binding is absent or ambiguous")
    binding = candidates[0]
    _registry(binding, authority_objects).require(binding, point)
    return binding


def _raw_receipt(store: _MemoryObjects, key: str, binding: CaptureBinding,
                 authority_objects: dict[str, bytes], budgets: RecoveryBudgets) -> dict[str, Any]:
    receipt = validate_bound_receipt(store, key, binding)
    registry = _registry(binding, authority_objects)
    for name in ("previous", "start", "end"):
        registry.require(binding, BoundCheckpoint.from_dict(receipt[name]))
    raw = receipt["raw"]
    if raw is not None:
        manifest = store.get_bounded(raw["manifest_key"], budgets.max_object_bytes)
        payload = store.get_bounded(raw["payload_key"], budgets.max_object_bytes)
        if _sha(manifest) != raw["manifest_sha256"] or _sha(payload) != raw["payload_sha256"]:
            raise ValueError("recovery receipt RAW SHA256 differs")
        batch = read_raw_batch(manifest, payload)
        start, end = (BoundCheckpoint.from_dict(receipt[name]) for name in ("start", "end"))
        if (batch.manifest.capture_binding != binding or batch.manifest.bound_high_watermark != end
                or len(batch.events) != receipt["event_count"] or any(
                    event.position.receiver != start.receiver_identity.receiver_name
                    or not start.sequence <= event.position.sequence <= end.sequence for event in batch.events)):
            raise ValueError("recovery receipt RAW context differs")
    return receipt


def _covered(point: BoundCheckpoint, binding: CaptureBinding, raw_groups: dict[str, dict[str, bytes]],
             authority_objects: dict[str, bytes], budgets: RecoveryBudgets) -> bool:
    if point == binding.bootstrap:
        return True
    for objects in raw_groups.values():
        receipts = {}
        store = _MemoryObjects(objects)
        for key, content in objects.items():
            if not re.fullmatch(r"receipts/scan-[a-f0-9]{64}\.json", key):
                continue
            record = _decode_checkpoint_json(content)
            if record.get("format_version") != "quadringent-scan-receipt-v3":
                continue
            if CaptureBinding.from_dict(record.get("capture_binding")) != binding:
                continue
            receipt = _raw_receipt(store, key, binding, authority_objects, budgets)
            previous = BoundCheckpoint.from_dict(receipt["previous"])
            if previous in receipts:
                raise ValueError("recovery scan chain forks")
            receipts[previous] = receipt
        current = binding.bootstrap
        visited = set()
        while current in receipts:
            if current in visited:
                raise ValueError("recovery scan chain cycles")
            visited.add(current)
            receipt = receipts[current]
            start, end = (BoundCheckpoint.from_dict(receipt[name]) for name in ("start", "end"))
            if point == end or (point.receiver_identity == start.receiver_identity
                                and start.sequence <= point.sequence <= end.sequence):
                return True
            current = end
    return False


def _contexts(checkpoints: dict[str, tuple[CaptureBinding, BoundCheckpoint]], authority_objects: dict[str, bytes],
              raw_groups: dict[str, dict[str, bytes]], budgets: RecoveryBudgets) -> None:
    for objects in raw_groups.values():
        store = _MemoryObjects(objects)
        for key, content in objects.items():
            match = re.fullmatch(r"batch-([a-f0-9]{32})\.manifest\.json", key)
            if match is None:
                continue
            payload = store.get_bounded("batch-" + match[1] + ".jsonl", budgets.max_object_bytes)
            batch = read_raw_batch(content, payload)
            binding = batch.manifest.capture_binding
            if binding is None:
                continue  # Preserve historical RAW without adopting a native identity.
            context = batch.manifest.copy_context
            if context is None:
                point = batch.manifest.bound_high_watermark
            else:
                point = BoundCheckpoint.from_dict(context["boundary"])
                intent_key = copy_intent_key(context["pipeline_id"], context["table_id"], context["run_id"])
                intent_content = authority_objects.get(intent_key)
                if intent_content is None:
                    raise ValueError("snapshot original copy intent is absent")
                intent = _decode_checkpoint_json(intent_content)
                boundary = JournalBoundary.from_dict(intent["boundary"])
                if boundary.capture_binding != binding or boundary.require_bound() != point:
                    raise ValueError("snapshot original copy intent boundary differs")
            _registry(binding, authority_objects).require(binding, point)
    class PrefixObjects:
        def __init__(self, prefix):
            if prefix not in raw_groups:
                raise ValueError('copy observation replay prefix is absent')
            self.objects = raw_groups[prefix]

        def get_bounded(self, key, maximum):
            data = self.objects.get(key)
            if data is None or len(data) > maximum:
                raise ValueError('copy observation replay original is absent or oversized')
            return data

    def original(key):
        matches = [content for prefix, values in raw_groups.items() for relative, content in values.items()
                   if prefix + '/' + relative == key]
        if len(matches) != 1:
            raise ValueError('copy observation original evidence is absent or ambiguous')
        return matches[0]

    from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, OBSERVED_EVIDENCE_FORMAT_VERSION, MAX_EVIDENCE_BYTES

    for prefix, values in raw_groups.items():
        for relative, content in values.items():
            if not relative.endswith('.json') or relative.endswith('.manifest.json'):
                continue
            if len(content) > MAX_EVIDENCE_BYTES:
                if '/evidence/' in prefix + '/' + relative:
                    raise ValueError('copy_observation_evidence_budget')
                continue
            try:
                record = _decode_checkpoint_json(content)
            except (ValueError, UnicodeError):
                if '/evidence/' in prefix + '/' + relative:
                    raise ValueError('copy_observation_evidence_unreadable') from None
                continue  # Historical opaque JSON-labelled material grants no authority.
            if type(record) is not dict or record.get('format_version') != OBSERVED_EVIDENCE_FORMAT_VERSION:
                continue
            evidence = InitialCopyEvidence.from_dict(record)
            manifest = read_copy_observations(_MemoryObjects(authority_objects), evidence.copy_observations)
            if manifest['evidence_key'] != prefix + '/' + relative:
                raise ValueError('copy_observation_evidence_namespace_mismatch')

    for key, content in authority_objects.items():
        if not key.startswith('copy-observations-v1/') or not key.endswith('/manifest.json'):
            continue
        metadata = _decode_checkpoint_json(content)
        if metadata.get('format_version') != MANIFEST_VERSION:
            raise ValueError('unknown copy observation manifest')
        reference = ObservationReference(key, _sha(content), len(content))
        manifest = read_copy_observations(_MemoryObjects(authority_objects), reference)
        evidence = InitialCopyEvidence.from_dict(_decode_checkpoint_json(original(manifest['evidence_key'])))
        context = manifest['context']
        if (evidence.copy_observations != reference or evidence.boundary.to_dict() != context['boundary']
                or (evidence.pipeline_id, evidence.table_id, evidence.run_id, evidence.rows_copied)
                != (context['pipeline_id'], context['table_id'], context['run_id'], manifest['rows_copied'])
                or [ref.to_dict() for ref in evidence.snapshot_batches]
                != [{k: v[k] for k in ('payload_key', 'manifest_key')} for v in manifest['snapshot_inventory']]):
            raise ValueError('restored copy observations differ from original copy evidence')
        validate_inventory_originals(manifest, PrefixObjects)

    for binding, point in checkpoints.values():
        if _binding_for(point, authority_objects) != binding:
            raise ValueError("checkpoint original capture binding differs")
        if not _covered(point, binding, raw_groups, authority_objects, budgets):
            raise ValueError("selected RAW prefixes do not cover the checkpoint from its original bootstrap")


def _no_symlinks(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for entry in (absolute, *absolute.parents):
        if entry.is_symlink():
            raise ValueError("recovery paths must not contain symbolic links")
    return absolute


def _new_directory(path: Path) -> Path:
    path = _no_symlinks(path)
    if path.exists() or not path.parent.is_dir():
        raise ValueError("recovery destination must be new with an existing parent directory")
    return path


def _private_read(path: Path, max_bytes: int) -> bytes:
    _no_symlinks(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("recovery files must be private regular files (0600)")
        if info.st_size > max_bytes:
            raise ValueError("recovery file exceeds byte budget")
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("recovery file exceeds byte budget")
    return content


def _write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reference(content: bytes) -> dict[str, Any]:
    return {"sha256": _sha(content), "bytes": len(content)}


def _check_total(authority: bytes, authority_objects: dict[str, bytes], raw_groups: dict[str, dict[str, bytes]],
                 checkpoint_bytes: dict[str, bytes], budgets: RecoveryBudgets) -> None:
    keys = len(authority_objects) + sum(map(len, raw_groups.values())) + len(checkpoint_bytes)
    size = len(authority) + sum(len(content) for objects in raw_groups.values() for content in objects.values())
    size += sum(map(len, checkpoint_bytes.values()))
    if keys > budgets.max_keys or size > budgets.max_total_bytes:
        raise ValueError("recovery bundle exceeds total key or byte budget")


def _receipt(state: str, digest: str, selection: RecoverySelection) -> dict[str, Any]:
    return {"state": state, "manifest_sha256": digest, "raw_prefixes": list(selection.raw_prefixes),
            "checkpoint_keys": list(selection.checkpoint_keys)}


def export_recovery_bundle(destination: Path, *, storage, selection: RecoverySelection,
                           writers_suspended: bool, cut_at: datetime | None = None,
                           budgets: RecoveryBudgets = RecoveryBudgets()) -> dict[str, Any]:
    _suspended(writers_suspended)
    _selection(selection, budgets, storage)
    destination = _new_directory(Path(destination))
    cut_at = datetime.now(timezone.utc) if cut_at is None else cut_at
    if type(cut_at) is not datetime or cut_at.utcoffset() != timedelta(0):
        raise ValueError("recovery cut time must be UTC")
    authority_store = storage.object_store("receiver-authority-v1")
    backup = ReceiverAuthorityBackup(authority_store, authority=selection.authority)
    authority = backup.export_inventory(writers_suspended=True, max_keys=budgets.max_keys,
                                         max_bytes=budgets.max_authority_bytes)
    authority_sha = _sha(authority)
    authority_objects = backup.validate_inventory(authority, expected_sha256=authority_sha,
                                                  max_keys=budgets.max_keys, max_bytes=budgets.max_authority_bytes)
    raw_groups = {}
    remaining_keys = budgets.max_keys - len(authority_objects) - len(selection.checkpoint_keys)
    remaining_bytes = budgets.max_total_bytes - len(authority)
    if remaining_keys < 0 or remaining_bytes < 0:
        raise ValueError("recovery bundle exceeds total key or byte budget")
    for prefix in selection.raw_prefixes:
        objects = _stable_objects(storage.object_store(prefix), budgets,
                                  max_keys=remaining_keys, max_bytes=remaining_bytes)
        raw_groups[prefix] = objects
        remaining_keys -= len(objects)
        remaining_bytes -= sum(map(len, objects.values()))
    checkpoints = {}
    checkpoint_bytes = {}
    for key in selection.checkpoint_keys:
        point = storage.checkpoint_store(key).load_bound()
        if type(point) is not BoundCheckpoint:
            raise ValueError("selected checkpoint is absent or is not bound v2")
        checkpoints[key] = (_binding_for(point, authority_objects), point)
        checkpoint_bytes[key] = _encode(point.to_dict())
    _check_total(authority, authority_objects, raw_groups, checkpoint_bytes, budgets)
    _contexts(checkpoints, authority_objects, raw_groups, budgets)
    backup.verify_inventory(authority, expected_sha256=authority_sha, max_keys=budgets.max_keys,
                            max_bytes=budgets.max_authority_bytes)
    for prefix, objects in raw_groups.items():
        if _stable_objects(storage.object_store(prefix), budgets) != objects:
            raise ValueError("RAW inventory changed during recovery export")
    for key, (_, point) in checkpoints.items():
        if storage.checkpoint_store(key).load_bound() != point:
            raise ValueError("checkpoint changed during recovery export")
    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION, "selection": selection.to_dict(),
        "scope_ids": sorted({point.scope_id for _, point in checkpoints.values()}),
        "cut_at_utc": cut_at.isoformat(), "writers_suspended": True, "limitations": list(_LIMITATIONS),
        "authority_inventory": _reference(authority),
        "raw_objects": [{"prefix": prefix, "objects": [{"key": key, **_reference(content)}
                                                      for key, content in objects.items()]}
                        for prefix, objects in raw_groups.items()],
        "checkpoints": [{"key": key, "capture_binding": checkpoints[key][0].to_dict(), **_reference(content)}
                        for key, content in checkpoint_bytes.items()],
    }
    payload = _encode(manifest)
    if len(payload) > budgets.max_manifest_bytes:
        raise ValueError("recovery manifest exceeds byte budget")
    blobs = {_sha(authority): authority}
    for objects in raw_groups.values():
        blobs.update({_sha(content): content for content in objects.values()})
    blobs.update({_sha(content): content for content in checkpoint_bytes.values()})
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    blob_dir = destination / "blobs"
    blob_dir.mkdir(mode=0o700)
    blob_dir.chmod(0o700)
    for sha, content in sorted(blobs.items()):
        _write(blob_dir / sha, content)
    _fsync_directory(blob_dir)
    _write(destination / "manifest.json", payload)
    _fsync_directory(destination)
    _fsync_directory(destination.parent)
    return _receipt("exported", _sha(payload), selection)


@dataclass(frozen=True)
class _Validated:
    manifest: dict[str, Any]
    selection: RecoverySelection
    authority: bytes
    raw_groups: dict[str, dict[str, bytes]]
    checkpoints: dict[str, tuple[CaptureBinding, BoundCheckpoint]]


def _validate_bundle(directory: Path, expected_sha256: str, budgets: RecoveryBudgets) -> _Validated:
    _digest(expected_sha256)
    directory = _no_symlinks(Path(directory))
    for path in (directory, directory / "blobs"):
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise ValueError("recovery directories must be private directories (0700)")
    if {entry.name for entry in directory.iterdir()} != {"manifest.json", "blobs"}:
        raise ValueError("unexpected files in recovery bundle")
    payload = _private_read(directory / "manifest.json", budgets.max_manifest_bytes)
    if _sha(payload) != expected_sha256:
        raise ValueError("recovery manifest SHA256 differs")
    manifest = _decode_checkpoint_json(payload)
    if (set(manifest) != {"format_version", "selection", "scope_ids", "cut_at_utc", "writers_suspended",
                          "limitations", "authority_inventory", "raw_objects", "checkpoints"}
            or manifest["format_version"] != BUNDLE_FORMAT_VERSION or manifest["writers_suspended"] is not True
            or manifest["limitations"] != list(_LIMITATIONS) or _encode(manifest) != payload):
        raise ValueError("recovery manifest format is invalid or unknown")
    selection = RecoverySelection.from_dict(manifest["selection"])
    _selection(selection, budgets)
    if type(manifest["cut_at_utc"]) is not str or datetime.fromisoformat(manifest["cut_at_utc"]).utcoffset() != timedelta(0):
        raise ValueError("recovery cut time must be UTC")
    if type(manifest["raw_objects"]) is not list or type(manifest["checkpoints"]) is not list:
        raise ValueError("invalid recovery inventories")
    if len(manifest["raw_objects"]) != len(selection.raw_prefixes) or len(manifest["checkpoints"]) != len(selection.checkpoint_keys):
        raise ValueError("recovery inventories differ from selection")
    blobs: dict[str, bytes] = {}
    size = 0
    def blob(reference, fields, limit):
        nonlocal size
        if type(reference) is not dict or set(reference) != fields:
            raise ValueError("invalid recovery object reference")
        sha, count = reference["sha256"], reference["bytes"]
        _digest(sha)
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError("invalid recovery object byte count")
        if sha not in blobs:
            content = _private_read(directory / "blobs" / sha, limit)
            if _sha(content) != sha:
                raise ValueError("recovery blob SHA256 differs")
            size += len(content)
            if size > budgets.max_total_bytes:
                raise ValueError("recovery bundle exceeds total byte budget")
            blobs[sha] = content
        content = blobs[sha]
        if len(content) != count:
            raise ValueError("recovery blob byte count differs")
        return content
    authority = blob(manifest["authority_inventory"], {"sha256", "bytes"}, budgets.max_authority_bytes)
    backup = ReceiverAuthorityBackup(_MemoryObjects({}), authority=selection.authority)
    authority_objects = backup.validate_inventory(authority, expected_sha256=_sha(authority),
                                                  max_keys=budgets.max_keys, max_bytes=budgets.max_authority_bytes)
    raw_groups = {}
    key_count = len(authority_objects) + len(selection.checkpoint_keys)
    for prefix, group in zip(selection.raw_prefixes, manifest["raw_objects"]):
        if type(group) is not dict or set(group) != {"prefix", "objects"} or group["prefix"] != prefix or type(group["objects"]) is not list:
            raise ValueError("recovery RAW inventory differs from selection")
        key_count += len(group["objects"])
        if key_count > budgets.max_keys:
            raise ValueError("recovery bundle exceeds key budget")
        objects = {}
        for entry in group["objects"]:
            content = blob(entry, {"key", "sha256", "bytes"}, budgets.max_object_bytes)
            key = _validate_object_key(entry["key"])
            if key in objects:
                raise ValueError("duplicate recovery RAW object")
            objects[key] = content
        if list(objects) != sorted(objects):
            raise ValueError("recovery RAW objects must be sorted")
        raw_groups[prefix] = objects
    checkpoints = {}
    checkpoint_bytes = {}
    for key, entry in zip(selection.checkpoint_keys, manifest["checkpoints"]):
        content = blob(entry, {"key", "capture_binding", "sha256", "bytes"}, budgets.max_object_bytes)
        if entry["key"] != key:
            raise ValueError("recovery checkpoint keys differ from selection")
        point = BoundCheckpoint.from_dict(_decode_checkpoint_json(content))
        if content != _encode(point.to_dict()):
            raise ValueError("noncanonical recovery checkpoint")
        checkpoints[key] = (CaptureBinding.from_dict(entry["capture_binding"]), point)
        checkpoint_bytes[key] = content
    if manifest["scope_ids"] != sorted({point.scope_id for _, point in checkpoints.values()}):
        raise ValueError("recovery scopes differ from checkpoints")
    if {entry.name for entry in (directory / "blobs").iterdir()} != set(blobs):
        raise ValueError("recovery blob inventory contains missing or extra objects")
    _check_total(authority, authority_objects, raw_groups, checkpoint_bytes, budgets)
    _contexts(checkpoints, authority_objects, raw_groups, budgets)
    return _Validated(manifest, selection, authority, raw_groups, checkpoints)


def verify_recovery_bundle(directory: Path, *, expected_sha256: str,
                           budgets: RecoveryBudgets = RecoveryBudgets()) -> dict[str, Any]:
    """Verify local bytes and context without consulting or changing any backend."""
    return _validate_bundle(directory, expected_sha256, budgets).manifest


def _verify_raw(storage, raw_groups: dict[str, dict[str, bytes]], budgets: RecoveryBudgets, *, subset: bool) -> None:
    for prefix, objects in raw_groups.items():
        existing = _stable_objects(storage.object_store(prefix), budgets)
        if (not subset and existing != objects) or any(key not in objects or content != objects[key]
                                                      for key, content in existing.items()):
            raise ValueError("recovery RAW collision or unexpected inventory")


def restore_recovery_bundle(directory: Path, *, expected_sha256: str, storage, selection: RecoverySelection,
                            writers_suspended: bool, budgets: RecoveryBudgets = RecoveryBudgets()) -> dict[str, Any]:
    """Import immutable bytes before absent/exact bound checkpoints; failures are retryable."""
    _suspended(writers_suspended)
    _selection(selection, budgets, storage)
    validated = _validate_bundle(directory, expected_sha256, budgets)
    if selection != validated.selection:
        raise ValueError("recovery labels or checkpoint keys differ from the original selection")
    backup = ReceiverAuthorityBackup(storage.object_store("receiver-authority-v1"), authority=selection.authority)
    options = {"expected_sha256": _sha(validated.authority), "max_keys": budgets.max_keys,
               "max_bytes": budgets.max_authority_bytes}
    backup.preflight_import(validated.authority, writers_suspended=True, **options)
    _verify_raw(storage, validated.raw_groups, budgets, subset=True)
    for key, (_, point) in validated.checkpoints.items():
        current = storage.checkpoint_store(key).load_bound()
        if current is not None and current != point:
            raise ValueError("recovery checkpoint collision; reset or migration is forbidden")
    backup.import_inventory(validated.authority, writers_suspended=True, **options)
    for prefix, objects in validated.raw_groups.items():
        for key, content in objects.items():
            storage.object_store(prefix).put_once(key, content)
    backup.verify_inventory(validated.authority, **options)
    _verify_raw(storage, validated.raw_groups, budgets, subset=False)
    # Recheck every checkpoint before the first CAS; individual backends fence absent writes.
    for key, (_, point) in validated.checkpoints.items():
        current = storage.checkpoint_store(key).load_bound()
        if current is not None and current != point:
            raise ValueError("checkpoint changed during recovery import")
    for key, (_, point) in validated.checkpoints.items():
        checkpoint = storage.checkpoint_store(key)
        if checkpoint.load_bound() is None:
            checkpoint.compare_and_set_bound(None, point)
        elif checkpoint.load_bound() != point:
            raise ValueError("checkpoint changed during recovery CAS")
    backup.verify_inventory(validated.authority, **options)
    _verify_raw(storage, validated.raw_groups, budgets, subset=False)
    for key, (_, point) in validated.checkpoints.items():
        if storage.checkpoint_store(key).load_bound() != point:
            raise ValueError("restored checkpoint differs; partial recovery did not complete")
    return _receipt("restored", expected_sha256, selection)
