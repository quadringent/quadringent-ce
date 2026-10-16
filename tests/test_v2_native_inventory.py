"""Capture real RAW batches, receipts and indexes as pinned archived originals."""

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from urllib.parse import urlsplit

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.object_store import RawFirstCaptureCoordinator
from quadringent.qualification.verification_stored import BatchReference, ObjectReference, export_stored
from quadringent.raw import RawBatchWriter, bind_raw_batch
from quadringent.storage_backend import StorageBackend
from quadringent.storage_layout import journal_prefix
from quadringent_control_plane.v2.services.native_artifacts import ArtifactArchive
from quadringent_control_plane.v2.services.native_inventory import InventoryLimits, NativeInventoryError, StoredRawInventory
from quadringent_control_plane.v2.services.native_resolution import PreparationResolver
from test_native_verification_stored import record
from test_v2_native_preparation import ROOT, case, preparer  # noqa: F401
from test_v2_native_verifications import authority  # noqa: F401


@pytest.fixture
def resolved(case):
    return PreparationResolver(case.storage, ROOT, "org-one").resolve(preparer(case).prepare(case.context), context=case.context)


def publish(case, scope, tmp_path, *, start=21, end=21, empty=False):
    batch = None
    if not empty:
        image = {"ID": 2, "AMOUNT": "1." + "0" * 12, "LONG_BUSINESS_LABEL": "SECOND  "}
        events = [record(scope, sequence, image) for sequence in range(start, end + 1)]
        staging = tmp_path / ("raw-stage-" + str(start))
        produced = RawBatchWriter(staging).write_batch(events, high_watermark=events[-1].position)
        payload = (staging / f"batch-{produced.batch_id}.jsonl").read_bytes()
        manifest = bind_raw_batch((staging / f"batch-{produced.batch_id}.manifest.json").read_bytes(), payload,
                                  binding=scope.capture_binding, end=scope.capture_binding.checkpoint(scope.checkpoint.receiver_identity, end))
        batch_id = json.loads(manifest)["batch_id"]
        prefix = journal_prefix(ROOT, scope.source_table)
        manifest_key, payload_key = f"{prefix}/batch-{batch_id}.manifest.json", f"{prefix}/batch-{batch_id}.jsonl"
        case.store.put_once(manifest_key, manifest)
        case.store.put_once(payload_key, payload)
        batch = BatchReference(ObjectReference(manifest_key, hashlib.sha256(manifest).hexdigest()),
                               ObjectReference(payload_key, hashlib.sha256(payload).hexdigest()))
    checkpoint = JsonCheckpointStore(tmp_path / ("capture-" + str(start) + ".json"))
    def point(sequence):
        return scope.capture_binding.checkpoint(scope.checkpoint.receiver_identity, sequence)
    checkpoint.compare_and_set_bound(None, point(start - 1))
    assert checkpoint.load_bound() == point(start - 1)
    store = case.storage.object_store(journal_prefix(ROOT, scope.source_table))
    RawFirstCaptureCoordinator(store, checkpoint).capture_receipted_window_result(
        start=point(start), end=point(end), previous=point(start - 1), capture_binding=scope.capture_binding,
        manifest_content=case.store.get_bounded(batch.manifest.key, 65536) if batch else None,
        payload=case.store.get_bounded(batch.payload.key, 65536) if batch else None,
        scan_completed_at=datetime(2026, 10, 16, 9, tzinfo=timezone.utc))
    return batch


def capture(case, resolved, **kwargs):
    return StoredRawInventory(case.storage, ROOT, "org-one", archive=ArtifactArchive(case.storage, "org-one"), **kwargs).capture(resolved, check_current=lambda: True)


def test_capture_archives_original_raw_receipt_index_and_exports_after_live_deletion(case, resolved, tmp_path):
    batch = publish(case, resolved.scope, tmp_path)
    result = capture(case, resolved)
    assert len(result.read_set.batches) == 2
    selected = next(b for b in result.read_set.batches if b.manifest.key == batch.manifest.key)
    assert selected.receipt is not None
    document = json.loads(case.store.get_bounded(urlsplit(result.inventory_artifact.uri).path.lstrip("/"), 1024**2))
    assert document["native_admission"] == "not_evaluated" and document["native_cdc_qualified"] is False
    assert document["coverage"] == "observed_keys_only"
    prefix = journal_prefix(ROOT, resolved.scope.source_table)
    for key in case.storage.object_store(prefix).list_keys(100):
        (case.store.root / prefix / key).unlink()
    output = BytesIO()
    export_stored(result.store, resolved.scope, output, dataset="raw", raw_prefix_root=ROOT,
                  initial_copy=resolved.initial_copy, copy_intent=resolved.copy_intent, raw_read_set=result.read_set)
    assert b'"dataset":"raw"' in output.getvalue()
    result.verify()


def test_inventory_records_full_outside_receipt_and_excludes_its_entire_batch(case, resolved, tmp_path):
    publish(case, resolved.scope, tmp_path, start=21)
    outside = publish(case, resolved.scope, tmp_path, start=22, end=22)
    result = capture(case, resolved)
    assert outside.manifest.key not in {b.manifest.key for b in result.read_set.batches}
    document = json.loads(case.store.get_bounded(urlsplit(result.inventory_artifact.uri).path.lstrip("/"), 1024**2))
    assert len([r for r in document["receipts"] if r["selection"] == "outside_after"]) == 1


def test_inventory_refuses_crossing_c_without_truncating_rows(case, resolved, tmp_path):
    publish(case, resolved.scope, tmp_path, start=21, end=22)
    with pytest.raises(NativeInventoryError):
        capture(case, resolved)


@pytest.mark.parametrize("kind", ["manifest", "receipt", "index", "unknown"])
def test_inventory_refuses_orphans_and_unknown_objects(case, resolved, tmp_path, kind):
    batch = publish(case, resolved.scope, tmp_path)
    prefix = journal_prefix(ROOT, resolved.scope.source_table)
    keys = case.storage.object_store(prefix).list_keys(100)
    if kind == "unknown":
        case.store.put_once(prefix + "/unrecognized.json", b"{}")
    else:
        key = batch.manifest.key if kind == "manifest" else prefix + "/" + next(k for k in keys if k.startswith("receipts/" if kind == "receipt" else "scan-index/"))
        (case.store.root / key).unlink()
    with pytest.raises(NativeInventoryError):
        capture(case, resolved)


def test_empty_scan_keeps_receipt_and_index_without_inventing_a_batch(case, resolved, tmp_path):
    publish(case, resolved.scope, tmp_path, empty=True)
    result = capture(case, resolved)
    assert len(result.read_set.batches) == 1
    document = json.loads(case.store.get_bounded(urlsplit(result.inventory_artifact.uri).path.lstrip("/"), 1024**2))
    assert document["receipts"][0]["event_count"] == 0
    result.verify()


def test_captured_original_mutation_and_unknown_live_key_never_fall_back(case, resolved, tmp_path):
    batch = publish(case, resolved.scope, tmp_path)
    result = capture(case, resolved)
    digest = hashlib.sha256(case.store.get_bounded(batch.payload.key, 65536)).hexdigest()
    key = urlsplit(result.inventory_artifact.uri).path.lstrip("/").rsplit("/", 1)[0] + "/" + digest
    (case.store.root / key).write_bytes(b"wrong")
    with pytest.raises(ValueError):
        result.store.get_bounded(batch.payload.key, 65536)
    with pytest.raises(NativeInventoryError):
        result.verify()
    with pytest.raises(ValueError):
        result.store.get_bounded("not-captured", 65536)


def test_capture_is_lazy_strictly_fenced_and_bounded(case, resolved, tmp_path, monkeypatch):
    publish(case, resolved.scope, tmp_path)
    archive = ArtifactArchive(case.storage, "org-one")
    with monkeypatch.context() as patch:
        patch.setattr(StorageBackend, "object_store", lambda *_: pytest.fail("unexpected IO"))
        configured = StoredRawInventory(case.storage, ROOT, "org-one", archive=archive)
        with pytest.raises(NativeInventoryError):
            configured.capture(resolved, check_current=lambda: 1)
    for limits in (InventoryLimits(max_keys=1), InventoryLimits(max_total_bytes=100), InventoryLimits(max_object_bytes=1)):
        with pytest.raises(NativeInventoryError):
            capture(case, resolved, limits=limits)


def test_inventory_detects_listing_changes(case, resolved, tmp_path, monkeypatch):
    publish(case, resolved.scope, tmp_path)
    from quadringent.object_store import FileObjectStore
    original = FileObjectStore.list_keys
    calls = []
    def changing(store, maximum):
        keys = original(store, maximum)
        calls.append(keys)
        return keys if len(calls) == 1 else keys[:-1]
    monkeypatch.setattr(FileObjectStore, "list_keys", changing)
    with pytest.raises(NativeInventoryError):
        capture(case, resolved)
