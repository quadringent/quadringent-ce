"""Capture observed RAW originals; an observed inventory does not admit a cut."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re

from quadringent.object_store import _validate_object_key, receipt_index_key, validate_bound_scan
from quadringent.proof_windows import validate_bound_receipt
from quadringent.qualification.verification_stored import BatchReference, ObjectReference, StoredReadSet
from quadringent.receiver_identity import BoundCheckpoint
from quadringent.storage_layout import journal_prefix, snapshot_prefix
from .native_artifacts import ArtifactArchive, storage_prefix
from .native_context import canonical
from .native_preparation import _json
from .native_resolution import PinnedArchiveStore, ReadBudget, ResolvedPreparation, ResolutionLimits
from .native_verifications import NativeArtifact


_MANIFEST = re.compile(r"batch-([0-9a-f]{32})\.manifest\.json")
_PAYLOAD = re.compile(r"batch-([0-9a-f]{32})\.jsonl")
_RECEIPT = re.compile(r"receipts/scan-[0-9a-f]{64}\.json")
_INDEX = re.compile(r"scan-index/end-[0-9a-f]{64}\.json")


class NativeInventoryError(ValueError):
    """An observed inventory was incomplete, unsupported or changed."""


def _require(condition):
    if not condition:
        raise NativeInventoryError("native_raw_inventory_invalid")


@dataclass(frozen=True)
class InventoryLimits:
    max_object_bytes: int = 32 * 1024**2
    max_total_bytes: int = 16 * 1024**3
    max_keys: int = 100_000
    max_seconds: int = 1800

    def __post_init__(self):
        for value, cap in ((self.max_object_bytes, 64 * 1024**2), (self.max_total_bytes, 16 * 1024**3),
                           (self.max_keys, 100_000), (self.max_seconds, 7200)):
            _require(type(value) is int and 1 <= value <= cap)


class _Prefix:
    def __init__(self, store, prefix):
        self.store, self.prefix = store, prefix

    def get_bounded(self, key, maximum):
        _validate_object_key(key)
        return self.store.get_bounded(self.prefix + "/" + key, maximum)


class _Overlay:
    def __init__(self, prepared, captured):
        _require(not set(prepared.references).intersection(captured.references))
        self.prepared, self.captured = prepared, captured

    def get_bounded(self, key, maximum):
        _validate_object_key(key)
        if key in self.prepared.references:
            return self.prepared.get_bounded(key, maximum)
        return self.captured.get_bounded(key, maximum)


@dataclass(frozen=True)
class CapturedRawInventory:
    read_set: StoredReadSet
    store: _Overlay
    inventory_artifact: NativeArtifact
    _resolved: ResolvedPreparation

    def verify(self):
        try:
            self._resolved.verify()
            captured = self.store.captured
            for key, reference in captured.references.items():
                captured.get_bounded(key, reference.byte_count)
            captured.read_reference(self.inventory_artifact, self.inventory_artifact.byte_count)
            captured.budget.check()
        except Exception:
            raise NativeInventoryError("native_raw_inventory_original_unavailable") from None


class StoredRawInventory:
    def __init__(self, storage, raw_prefix_root, org_id, *, archive, limits=InventoryLimits()):
        self.uri, self.prefix = storage_prefix(storage, org_id, "native-collections-v1")
        _validate_object_key(raw_prefix_root)
        _require(type(archive) is ArtifactArchive and type(limits) is InventoryLimits
                 and archive.storage is storage and archive.org_id == org_id)
        self.storage, self.root, self.org_id, self.archive, self.limits = storage, raw_prefix_root, org_id, archive, limits

    def capture(self, resolved, *, check_current):
        try:
            return self._capture(resolved, check_current)
        except Exception:
            raise NativeInventoryError("native_raw_inventory_capture_failed") from None

    def _capture(self, resolved, check_current):
        _require(type(resolved) is ResolvedPreparation
                 and (resolved.org_id, resolved.raw_prefix_root, resolved.backend, resolved.raw_bucket)
                 == (self.org_id, self.root, self.storage.kind, self.storage.raw_bucket))
        budget = ReadBudget(ResolutionLimits(max_object_bytes=self.limits.max_object_bytes,
                                             max_total_bytes=self.limits.max_total_bytes,
                                             max_seconds=self.limits.max_seconds), check_current)
        resolved.verify()
        scope = resolved.scope
        _require(len(scope.receiver_windows) == 1
                 and scope.copy_boundary.receiver_identity == scope.checkpoint.receiver_identity)
        prefix = journal_prefix(self.root, scope.source_table)
        budget.check()
        live = self.storage.object_store(prefix)

        def listing():
            budget.check()
            keys = live.list_keys(self.limits.max_keys)
            budget.check()
            _require(type(keys) is tuple and len(keys) <= self.limits.max_keys and len(set(keys)) == len(keys))
            for key in keys:
                _validate_object_key(key)
                _require(any(pattern.fullmatch(key) for pattern in (_MANIFEST, _PAYLOAD, _RECEIPT, _INDEX)))
            return tuple(sorted(keys))

        keys = listing()
        references = {}
        for index, key in enumerate(keys):
            content = budget.read(live, key, self.limits.max_object_bytes)
            references[prefix + "/" + key] = self.archive.retain_bytes("raw-original:" + str(index), content)
            budget.check()
        captured = PinnedArchiveStore(self.storage, self.uri, self.prefix, references, budget)
        overlay = _Overlay(resolved.store, captured)
        read_set, observations = inspect_raw_originals(
            resolved, self.root, keys, captured, check_current=budget.check, limits=self.limits)
        _require(listing() == keys)
        for key in keys:
            actual = budget.read(live, key, self.limits.max_object_bytes)
            reference = references[prefix + "/" + key]
            _require(len(actual) == reference.byte_count and hashlib.sha256(actual).hexdigest() == reference.sha256)
        _require(listing() == keys)
        document = {"format_version": "quadringent-native-raw-inventory-v1", "coverage": "observed_keys_only",
                    "native_admission": "not_evaluated", "native_cdc_qualified": False,
                    "org_id": self.org_id, "scope_sha256": scope.sha256, "context_sha256": resolved.context_sha256,
                    "prefix": self.uri + prefix + "/", "receipts": observations,
                    "originals": [{"source_key": key, "artifact": ref.to_dict()} for key, ref in sorted(references.items())],
                    "batches": [{"manifest": {"key": b.manifest.key, "sha256": b.manifest.sha256},
                                 "payload": {"key": b.payload.key, "sha256": b.payload.sha256},
                                 "receipt": {"key": b.receipt.key, "sha256": b.receipt.sha256} if b.receipt else None}
                                for b in read_set.batches]}
        inventory = self.archive.retain_bytes("raw_inventory", (canonical(document, maximum=self.limits.max_object_bytes) + "\n").encode())
        result = CapturedRawInventory(read_set, overlay, inventory, resolved)
        result.verify()
        return result


def inspect_raw_originals(resolved, raw_prefix_root, keys, captured, *, check_current, limits=InventoryLimits()):
    """Rebuild receipt/index selection from originals; listing coverage stays observed."""
    _require(type(resolved) is ResolvedPreparation and type(limits) is InventoryLimits)
    def check():
        _require(check_current() is True)
    check()
    scope = resolved.scope
    _require(len(scope.receiver_windows) == 1
             and scope.copy_boundary.receiver_identity == scope.checkpoint.receiver_identity)
    window = scope.receiver_windows[0]
    prefix = journal_prefix(raw_prefix_root, scope.source_table)
    _require(type(keys) is tuple and len(keys) <= limits.max_keys
             and keys == tuple(sorted(set(keys))))
    for key in keys:
        _validate_object_key(key)
        _require(any(pattern.fullmatch(key) for pattern in (_MANIFEST, _PAYLOAD, _RECEIPT, _INDEX)))
    scoped_store = _Prefix(captured, prefix)
    manifests = {k for k in keys if _MANIFEST.fullmatch(k)}
    payloads = {k for k in keys if _PAYLOAD.fullmatch(k)}
    receipts = {k for k in keys if _RECEIPT.fullmatch(k)}
    indexes = {k for k in keys if _INDEX.fullmatch(k)}
    _require(payloads == {k.removesuffix(".manifest.json") + ".jsonl" for k in manifests})
    used_manifests, used_indexes = set(), set()
    batches, observations = [], []
    for key in sorted(receipts):
        receipt_raw = scoped_store.get_bounded(key, 65536)
        _json(receipt_raw)
        receipt = validate_bound_receipt(scoped_store, key, scope.capture_binding)
        start, end, previous = (BoundCheckpoint.from_dict(receipt[n]) for n in ("start", "end", "previous"))
        _require(start.receiver_identity == window.identity == end.receiver_identity)
        index_key = receipt_index_key(end)
        _require(index_key in indexes and index_key not in used_indexes)
        _json(scoped_store.get_bounded(index_key, 4096))
        used_indexes.add(index_key)
        if end.sequence < window.start_sequence:
            selection = "outside_before"
        elif start.sequence > window.end_sequence:
            selection = "outside_after"
        else:
            _require(window.start_sequence <= start.sequence <= end.sequence <= window.end_sequence)
            selection = "selected"
        raw = receipt["raw"]
        manifest_content = payload_content = None
        if raw is not None:
            manifest_key, payload_key = raw["manifest_key"], raw["payload_key"]
            _require(manifest_key in manifests and manifest_key not in used_manifests and payload_key in payloads)
            used_manifests.add(manifest_key)
            manifest_content = scoped_store.get_bounded(manifest_key, limits.max_object_bytes)
            payload_content = scoped_store.get_bounded(payload_key, limits.max_object_bytes)
            _json(manifest_content)
            _require(hashlib.sha256(manifest_content).hexdigest() == raw["manifest_sha256"]
                     and hashlib.sha256(payload_content).hexdigest() == raw["payload_sha256"])
            if selection == "selected":
                batches.append(BatchReference(ObjectReference(prefix + "/" + manifest_key, raw["manifest_sha256"]),
                                              ObjectReference(prefix + "/" + payload_key, raw["payload_sha256"]),
                                              ObjectReference(prefix + "/" + key, hashlib.sha256(receipt_raw).hexdigest())))
        batch = validate_bound_scan(start=start, end=end, previous=previous, capture_binding=scope.capture_binding,
                                    manifest_content=manifest_content, payload=payload_content,
                                    scan_completed_at=datetime.fromisoformat(receipt["scan_completed_at"]))
        _require(receipt["event_count"] == (len(batch.events) if batch else 0))
        if batch:
            _require(batch.manifest.format_version == "as400-raw-v2"
                     and raw["manifest_key"] == "batch-" + batch.manifest.batch_id + ".manifest.json")
        observations.append({"key": prefix + "/" + key, "selection": selection, "event_count": receipt["event_count"],
                             "start": start.to_dict(), "end": end.to_dict()})
        check()
    _require(used_manifests == manifests and used_indexes == indexes)
    evidence = _json(resolved.store.get_bounded(resolved.initial_copy.key, 65536))
    snapshot = snapshot_prefix(raw_prefix_root, scope.source_table)
    snapshot_batches = []
    for ref in evidence["snapshot_batches"]:
        manifest_key, payload_key = snapshot + "/" + ref["manifest_key"], snapshot + "/" + ref["payload_key"]
        snapshot_batches.append(BatchReference(
            ObjectReference(manifest_key, resolved.store.references[manifest_key].sha256),
            ObjectReference(payload_key, resolved.store.references[payload_key].sha256)))
    read_set = StoredReadSet(scope.sha256, tuple(snapshot_batches + batches))
    check()
    return read_set, observations
