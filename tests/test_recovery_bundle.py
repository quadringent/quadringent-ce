from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.raw import RawBatchWriter, bind_raw_batch, bind_snapshot_batch
from quadringent.recovery_bundle import (
    RecoveryBudgets, RecoverySelection, export_recovery_bundle,
    restore_recovery_bundle, verify_recovery_bundle,
)
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import copy_intent_key


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
RUN = "00000000-0000-4000-8000-000000000001"
AUTHORITY = "s3://recovery-bucket/receiver-authority-v1"
IDENTITY = ReceiverIdentity("LIB", "JRN", "LIB", "RCV1", "offline-test-v1", "native-1")
SELECTION = RecoverySelection(AUTHORITY, ("raw/journal", "raw/table"), ("source", "table", "destination"))


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


class LocalStorage:
    kind = "aws"
    raw_bucket = "recovery-bucket"

    def __init__(self, root):
        self.root = root
        self.objects = {}
        self.checkpoints = {}

    def object_store(self, prefix):
        return self.objects.setdefault(prefix, FileObjectStore(self.root / "objects" / prefix))

    def checkpoint_store(self, key):
        return self.checkpoints.setdefault(key, JsonCheckpointStore(self.root / "checkpoints" / (key + ".json")))


def populated(tmp_path):
    storage = LocalStorage(tmp_path / "source")
    authority = storage.object_store("receiver-authority-v1")
    registry = ReceiverNameRegistry(authority, authority=AUTHORITY, source_system="ibmi", journal="JRN")
    binding = registry.create_or_load_binding(scope_id="scope", identity=IDENTITY, sequence=1)
    registry.reserve_legacy(JournalPosition("OLD", 42), original_proof_sha256="a" * 64)
    orphan_binding = replace(binding, scope_id="orphan", bootstrap=replace(binding.bootstrap, scope_id="orphan"))
    registry._claim_point(orphan_binding, orphan_binding.checkpoint(replace(IDENTITY, receiver_name="ORPHAN"), 1))
    other = ReceiverNameRegistry(authority, authority=AUTHORITY, source_system="other", journal="OTHER")
    other.create_or_load_binding(scope_id="other", identity=replace(IDENTITY, journal_name="OTHER"), sequence=0)
    boundary = JournalBoundary("LIB", "RCV1", 5, NOW, IDENTITY).with_binding(binding)
    intent = {"format_version": "quadringent-copy-intent-v1", "pipeline_id": "pipeline",
              "table_id": "table", "run_id": RUN, "boundary": boundary.to_dict()}
    authority.put_once(copy_intent_key("pipeline", "table", RUN), encoded(intent))
    for prefix in SELECTION.raw_prefixes:
        cp = JsonCheckpointStore(tmp_path / (prefix.replace("/", "-") + ".json"))
        cp.compare_and_set_bound(None, binding.bootstrap)
        event = ChangeEvent("ibmi", "JRN", "LIB", "TABLE", "c", JournalPosition("RCV1", 2),
                            NOW.isoformat(), "schema-v1", None, {"ID": 1})
        output = tmp_path / ("writer-" + prefix.replace("/", "-"))
        manifest = RawBatchWriter(output).write_batch([event], high_watermark=JournalPosition("RCV1", 10))
        payload = (output / f"batch-{manifest.batch_id}.jsonl").read_bytes()
        raw_manifest = bind_raw_batch(encoded(manifest.to_record()), payload,
                                      binding=binding, end=binding.checkpoint(IDENTITY, 10))
        RawFirstCaptureCoordinator(storage.object_store(prefix), cp).capture_receipted_window_result(
            start=binding.checkpoint(IDENTITY, 2), end=binding.checkpoint(IDENTITY, 10),
            previous=binding.bootstrap, scan_completed_at=NOW, capture_binding=binding,
            manifest_content=raw_manifest, payload=payload,
        )
        snapshot_position = JournalPosition("SNAPSHOT:" + RUN, 1)
        event = replace(event, journal="SNAPSHOT:LIB.TABLE", position=snapshot_position)
        snapshot = RawBatchWriter(output).write_batch([event], high_watermark=snapshot_position)
        payload = (output / f"batch-{snapshot.batch_id}.jsonl").read_bytes()
        raw_manifest = bind_snapshot_batch(encoded(snapshot.to_record()), payload,
                                           binding=binding, boundary=boundary.require_bound(),
                                           pipeline_id="pipeline", table_id="table", run_id=RUN)
        batch_id = json.loads(raw_manifest)["batch_id"]
        storage.object_store(prefix).put_once(f"batch-{batch_id}.manifest.json", raw_manifest)
        storage.object_store(prefix).put_once(f"batch-{batch_id}.jsonl", payload)
        storage.object_store(prefix).put_once("windows/historical/opaque.json", b"historical bytes\n")
        storage.object_store(prefix).put_once("snapshot/empty.bin", b"")
    for key, seq in (("source", 10), ("table", 5), ("destination", 7)):
        storage.checkpoint_store(key).compare_and_set_bound(None, binding.checkpoint(IDENTITY, seq))
    return storage, registry, binding


def inventory(storage):
    return {prefix: {key: store.get(key) for key in store.list_keys(1000)}
            for prefix, store in storage.objects.items()}


def exported(tmp_path):
    storage, registry, binding = populated(tmp_path)
    directory = tmp_path / "bundle"
    receipt = export_recovery_bundle(directory, storage=storage, selection=SELECTION,
                                     writers_suspended=True, cut_at=NOW)
    return storage, registry, binding, directory, receipt["manifest_sha256"]


def test_complete_loss_roundtrip_preserves_authority_raw_bound_checkpoints_and_private_modes(tmp_path):
    source, registry, binding, directory, sha = exported(tmp_path)
    manifest = verify_recovery_bundle(directory, expected_sha256=sha)
    assert manifest["format_version"] == "quadringent-native-recovery-bundle-v1"
    assert manifest["selection"]["authority"] == AUTHORITY
    assert manifest["writers_suspended"] is True
    assert manifest["scope_ids"] == [binding.scope_id]
    assert "Snowflake" in " ".join(manifest["limitations"])
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.rglob("*") if p.is_file())
    target = LocalStorage(tmp_path / "target")
    receipt = restore_recovery_bundle(directory, expected_sha256=sha, storage=target,
                                      selection=SELECTION, writers_suspended=True)
    assert receipt == {"state": "restored", "manifest_sha256": sha,
                       "raw_prefixes": list(SELECTION.raw_prefixes), "checkpoint_keys": list(SELECTION.checkpoint_keys)}
    assert inventory(target) == inventory(source)
    assert registry.claim_key("OLD") in inventory(target)["receiver-authority-v1"]
    assert registry.claim_key("ORPHAN") in inventory(target)["receiver-authority-v1"]
    for key in SELECTION.checkpoint_keys:
        assert target.checkpoint_store(key).load_bound() == source.checkpoint_store(key).load_bound()
    before = inventory(target)
    restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert inventory(target) == before


@pytest.mark.parametrize("attestation", [False, None, 1, "true"])
def test_export_restore_require_explicit_quiescence(tmp_path, attestation):
    source, _, _, directory, sha = exported(tmp_path)
    with pytest.raises(ValueError, match="suspend"):
        export_recovery_bundle(tmp_path / "no", storage=source, selection=SELECTION, writers_suspended=attestation)
    target = LocalStorage(tmp_path / "target")
    with pytest.raises(ValueError, match="suspend"):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target,
                                selection=SELECTION, writers_suspended=attestation)
    assert not target.objects and not target.checkpoints


@pytest.mark.parametrize("damage", ["tamper", "missing", "extra", "symlink", "mode"])
def test_invalid_local_bundle_refuses_before_target_effects(tmp_path, damage):
    _, _, _, directory, sha = exported(tmp_path)
    blob = next((directory / "blobs").iterdir())
    if damage == "tamper":
        blob.write_bytes(b"tampered")
    elif damage == "missing":
        blob.unlink()
    elif damage == "extra":
        (directory / "blobs" / ("a" * 64)).write_bytes(b"extra")
    elif damage == "symlink":
        content = blob.read_bytes()
        blob.unlink()
        sentinel = tmp_path / "sentinel"
        sentinel.write_bytes(content)
        blob.symlink_to(sentinel)
    else:
        blob.chmod(0o644)
    target = LocalStorage(tmp_path / "target")
    with pytest.raises((ValueError, FileNotFoundError)):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target,
                                selection=SELECTION, writers_suspended=True)
    assert not target.objects and not target.checkpoints


def test_external_manifest_sha_is_required_and_checked(tmp_path):
    _, _, _, directory, sha = exported(tmp_path)
    for invalid in (None, "", "A" * 64, "0" * 64):
        with pytest.raises(ValueError):
            verify_recovery_bundle(directory, expected_sha256=invalid)
    assert verify_recovery_bundle(directory, expected_sha256=sha)


@pytest.mark.parametrize("collision", ["authority", "raw", "extra_raw", "checkpoint", "legacy_checkpoint"])
def test_all_target_collisions_are_preflighted_before_immutable_writes_or_cp_advance(tmp_path, collision):
    source, registry, binding, directory, sha = exported(tmp_path)
    target = LocalStorage(tmp_path / "target")
    if collision == "authority":
        target.object_store("receiver-authority-v1").put_once(registry.binding_key(binding.scope_id), b"collision")
    elif collision in ("raw", "extra_raw"):
        source_raw = source.object_store(SELECTION.raw_prefixes[0])
        key = next(iter(source_raw.list_keys(100))) if collision == "raw" else "extra.json"
        target.object_store(SELECTION.raw_prefixes[0]).put_once(key, b"collision")
    elif collision == "checkpoint":
        target.checkpoint_store("destination").compare_and_set_bound(None, binding.checkpoint(IDENTITY, 8))
    else:
        target.checkpoint_store("destination").compare_and_set(None, JournalPosition("RCV1", 7))
    before = inventory(target)
    with pytest.raises(ValueError):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert inventory(target) == {**before, **{p: {} for p in target.objects if p not in before}}
    assert target.checkpoint_store("source").load_bound() is None


def test_restore_refuses_authority_or_prefix_or_checkpoint_migration(tmp_path):
    _, _, _, directory, sha = exported(tmp_path)
    target = LocalStorage(tmp_path / "target")
    for changed in (replace(SELECTION, authority=AUTHORITY.replace("recovery-bucket", "other-bucket")),
                    replace(SELECTION, raw_prefixes=("renamed",)), replace(SELECTION, checkpoint_keys=("renamed",))):
        with pytest.raises(ValueError):
            restore_recovery_bundle(directory, expected_sha256=sha, storage=target,
                                    selection=changed, writers_suspended=True)
    assert not target.objects and not target.checkpoints


@pytest.mark.parametrize("mutation", ["raw_bytes", "raw_keys", "checkpoint"])
def test_export_detects_concurrent_inventory_or_checkpoint_changes(tmp_path, monkeypatch, mutation):
    source, _, binding = populated(tmp_path)
    if mutation.startswith("raw"):
        store = source.object_store(SELECTION.raw_prefixes[0])
        original = store.list_keys
        calls = 0
        def changed(max_keys):
            nonlocal calls
            calls += 1
            if calls == 2:
                if mutation == "raw_keys":
                    store.put_once("late.bin", b"late")
                else:
                    (store.root / "windows/historical/opaque.json").write_bytes(b"changed")
            return original(max_keys)
        monkeypatch.setattr(store, "list_keys", changed)
    else:
        cp = source.checkpoint_store("source")
        original = JsonCheckpointStore.load_bound
        calls = 0
        def changed(self):
            nonlocal calls
            if self.path != cp.path:
                return original(self)
            calls += 1
            if calls == 2:
                cp.compare_and_set_bound(binding.checkpoint(IDENTITY, 10), binding.checkpoint(IDENTITY, 11))
            return original(self)
        monkeypatch.setattr(JsonCheckpointStore, "load_bound", changed)
    directory = tmp_path / "bundle"
    with pytest.raises(ValueError):
        export_recovery_bundle(directory, storage=source, selection=SELECTION, writers_suspended=True)
    assert not (directory / "manifest.json").exists()


def test_wrong_raw_selection_and_unclaimed_or_legacy_checkpoint_refuse(tmp_path):
    source, registry, binding = populated(tmp_path)
    with pytest.raises(ValueError, match="cover"):
        export_recovery_bundle(tmp_path / "wrong", storage=source,
                               selection=replace(SELECTION, raw_prefixes=("unused",)), writers_suspended=True)
    (source.object_store("receiver-authority-v1").root / registry.claim_key("RCV1")).unlink()
    with pytest.raises(ValueError):
        export_recovery_bundle(tmp_path / "orphan", storage=source, selection=SELECTION, writers_suspended=True)
    source.checkpoint_store("legacy").compare_and_set(None, JournalPosition("RCV1", 10))
    with pytest.raises(ValueError):
        export_recovery_bundle(tmp_path / "legacy", storage=source,
                               selection=replace(SELECTION, checkpoint_keys=("legacy",)), writers_suspended=True)
    assert binding.bootstrap.sequence == 1


def test_restore_retry_after_partial_immutable_and_checkpoint_failure(tmp_path, monkeypatch):
    source, _, _, directory, sha = exported(tmp_path)
    target = LocalStorage(tmp_path / "target")
    raw = target.object_store(SELECTION.raw_prefixes[0])
    original_put = raw.put_once
    failed = False
    def fail_once(key, content):
        nonlocal failed
        if not failed:
            failed = True
            original_put(key, content)
            raise RuntimeError("injected immutable failure")
        return original_put(key, content)
    monkeypatch.setattr(raw, "put_once", fail_once)
    with pytest.raises(RuntimeError):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert all(target.checkpoint_store(k).load_bound() is None for k in SELECTION.checkpoint_keys)
    cp = target.checkpoint_store("table")
    original_cas = JsonCheckpointStore.compare_and_set_bound
    def fail_cas(self, previous, current):
        if self.path == cp.path:
            raise RuntimeError("injected CAS failure")
        return original_cas(self, previous, current)
    monkeypatch.setattr(JsonCheckpointStore, "compare_and_set_bound", fail_cas)
    with pytest.raises(RuntimeError):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert target.checkpoint_store("source").load_bound() == source.checkpoint_store("source").load_bound()
    assert cp.load_bound() is None
    monkeypatch.setattr(JsonCheckpointStore, "compare_and_set_bound", original_cas)
    receipt = restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert receipt["state"] == "restored"
    assert inventory(target) == inventory(source)


def test_added_target_object_after_import_blocks_checkpoint_advance(tmp_path, monkeypatch):
    _, _, _, directory, sha = exported(tmp_path)
    target = LocalStorage(tmp_path / "target")
    store = target.object_store(SELECTION.raw_prefixes[0])
    original = store.put_once
    def added(key, content):
        result = original(key, content)
        original("surprise.json", b"surprise")
        return result
    monkeypatch.setattr(store, "put_once", added)
    with pytest.raises(ValueError):
        restore_recovery_bundle(directory, expected_sha256=sha, storage=target, selection=SELECTION, writers_suspended=True)
    assert all(target.checkpoint_store(k).load_bound() is None for k in SELECTION.checkpoint_keys)


def test_export_rechecks_listing_after_last_raw_reread(tmp_path, monkeypatch):
    source, _, _ = populated(tmp_path)
    store = source.object_store(SELECTION.raw_prefixes[0])
    final_read = len(store.list_keys(100)) * 4
    original = store.get_bounded
    calls = 0
    def changed(key, max_bytes):
        nonlocal calls
        calls += 1
        content = original(key, max_bytes)
        if calls == final_read:
            store.put_once("arrived-at-last-read.bin", b"late")
        return content
    monkeypatch.setattr(store, "get_bounded", changed)
    with pytest.raises(ValueError, match="changed"):
        export_recovery_bundle(tmp_path / "bundle", storage=source, selection=SELECTION, writers_suspended=True)
    assert not (tmp_path / "bundle" / "manifest.json").exists()


@pytest.mark.parametrize("prefixes,keys", [(("raw/",), ("cp",)), (("raw", "raw/sub"), ("cp",)),
                                         (("receiver-authority-v1/sub",), ("cp",)), (("../raw",), ("cp",)),
                                         (("raw",), ("cp", "cp")), (("raw",), ("",)), ((), ("cp",))])
def test_selection_refuses_aliases_overlap_empty_or_duplicate_keys(prefixes, keys):
    with pytest.raises(ValueError):
        RecoverySelection(AUTHORITY, prefixes, keys)


def test_new_destination_no_symlink_ancestors_and_bounded_budget(tmp_path):
    source, _, _ = populated(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError):
        export_recovery_bundle(existing, storage=source, selection=SELECTION, writers_suspended=True)
    alias = tmp_path / "alias"
    alias.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError):
        export_recovery_bundle(alias / "bundle", storage=source, selection=SELECTION, writers_suspended=True)
    with pytest.raises(ValueError):
        export_recovery_bundle(tmp_path / "tiny", storage=source, selection=SELECTION,
                               writers_suspended=True, budgets=RecoveryBudgets(max_total_bytes=10))
    assert not (tmp_path / "tiny" / "manifest.json").exists()


def test_snapshot_without_original_copy_intent_is_rejected_before_effects(tmp_path):
    source, _, _ = populated(tmp_path)
    key = copy_intent_key("pipeline", "table", RUN)
    (source.object_store("receiver-authority-v1").root / key).unlink()
    with pytest.raises(ValueError, match="intent"):
        export_recovery_bundle(tmp_path / "bundle", storage=source, selection=SELECTION, writers_suspended=True)
    assert not (tmp_path / "bundle" / "manifest.json").exists()


def test_manifest_with_rehashed_wrong_generation_checkpoint_is_still_refused(tmp_path):
    _, _, _, directory, _ = exported(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    entry = manifest["checkpoints"][0]
    old = directory / "blobs" / entry["sha256"]
    point = json.loads(old.read_bytes())
    point["receiver_identity"]["native_generation"] = "replacement"
    payload = encoded(point)
    entry.update(sha256=digest(payload), bytes=len(payload))
    blob = directory / "blobs" / entry["sha256"]
    blob.write_bytes(payload)
    blob.chmod(0o600)
    old.unlink()
    manifest_payload = encoded(manifest)
    (directory / "manifest.json").write_bytes(manifest_payload)
    target = LocalStorage(tmp_path / "target")
    with pytest.raises(ValueError, match="identity"):
        restore_recovery_bundle(directory, expected_sha256=digest(manifest_payload), storage=target,
                                selection=SELECTION, writers_suspended=True)
    assert not target.objects and not target.checkpoints


@pytest.mark.parametrize("location", ["manifest", "blob"])
@pytest.mark.parametrize("operation", ["verify", "restore"])
def test_fifo_is_refused_without_blocking_or_target_effects(tmp_path, location, operation):
    _, _, _, directory, sha = exported(tmp_path)
    path = directory / "manifest.json" if location == "manifest" else next((directory / "blobs").iterdir())
    path.unlink()
    os.mkfifo(path, 0o600)
    script = """
from pathlib import Path
import sys
from quadringent.recovery_bundle import RecoverySelection, restore_recovery_bundle, verify_recovery_bundle
class ForbiddenStorage:
    kind = 'aws'
    raw_bucket = 'recovery-bucket'
    def object_store(self, prefix):
        raise AssertionError('backend effect before validation')
    def checkpoint_store(self, key):
        raise AssertionError('checkpoint effect before validation')
selection = RecoverySelection('s3://recovery-bucket/receiver-authority-v1',
                              ('raw/journal', 'raw/table'), ('source', 'table', 'destination'))
try:
    if sys.argv[3] == 'verify':
        verify_recovery_bundle(Path(sys.argv[1]), expected_sha256=sys.argv[2])
    else:
        restore_recovery_bundle(Path(sys.argv[1]), expected_sha256=sys.argv[2], storage=ForbiddenStorage(),
                                selection=selection, writers_suspended=True)
except ValueError as error:
    assert 'regular' in str(error)
else:
    raise AssertionError('FIFO accepted')
"""
    result = subprocess.run([sys.executable, "-c", script, str(directory), sha, operation],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stderr


def test_large_real_raw_manifest_roundtrip_honors_configured_object_budget(tmp_path):
    source, _, binding = populated(tmp_path)
    output = tmp_path / "large-writer"
    events = [ChangeEvent("ibmi", "JRN", "LIB", "TABLE", "c", JournalPosition("RCV1", sequence),
                          NOW.isoformat(), "schema-v1", None, {"ID": sequence})
              for sequence in range(11, 1011)]
    end = binding.checkpoint(IDENTITY, 1010)
    manifest = RawBatchWriter(output).write_batch(events, high_watermark=end.position)
    payload = (output / f"batch-{manifest.batch_id}.jsonl").read_bytes()
    raw_manifest = bind_raw_batch(encoded(manifest.to_record()), payload, binding=binding, end=end)
    assert len(raw_manifest) > 65536
    RawFirstCaptureCoordinator(source.object_store(SELECTION.raw_prefixes[0]), source.checkpoint_store("source")).capture_receipted_window_result(
        start=binding.checkpoint(IDENTITY, 11), end=end, previous=binding.checkpoint(IDENTITY, 10),
        manifest_content=raw_manifest, payload=payload, scan_completed_at=NOW, capture_binding=binding,
    )
    directory = tmp_path / "bundle"
    receipt = export_recovery_bundle(directory, storage=source, selection=SELECTION, writers_suspended=True)
    sha = receipt["manifest_sha256"]
    assert verify_recovery_bundle(directory, expected_sha256=sha)
    target = LocalStorage(tmp_path / "target")
    restore_recovery_bundle(directory, expected_sha256=sha, storage=target,
                            selection=SELECTION, writers_suspended=True)
    assert inventory(target) == inventory(source)
    assert target.checkpoint_store("source").load_bound() == end
    limited = RecoveryBudgets(max_object_bytes=len(raw_manifest) - 1)
    with pytest.raises(ValueError, match="budget"):
        export_recovery_bundle(tmp_path / "too-small", storage=source, selection=SELECTION,
                               writers_suspended=True, budgets=limited)
    with pytest.raises(ValueError, match="(budget|byte count)"):
        verify_recovery_bundle(directory, expected_sha256=sha, budgets=limited)
