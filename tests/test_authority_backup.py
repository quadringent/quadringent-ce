from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from unittest.mock import Mock

import pytest

from quadringent.authority_backup import ReceiverAuthorityBackup
from quadringent.contract import JournalPosition
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import copy_intent_key
from test_object_listing import listing_store  # noqa: F401


AUTHORITY = "s3://registry-bucket/receiver-authority-v1"
IDENTITY = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "offline-test-v1", "native-a")


def encoded(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


class MemoryAuthority:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.writes = []
        self.on_list = None
        self.on_read = None
        self.after_put = None

    def list_keys(self, max_keys):
        if self.on_list:
            self.on_list(self)
        if len(self.objects) > max_keys:
            raise ValueError("listing exceeds budget")
        return tuple(sorted(self.objects))

    def get_bounded(self, key, max_bytes):
        if self.on_read:
            self.on_read(self, key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        content = self.objects[key]
        if len(content) > max_bytes:
            raise ValueError("object exceeds read budget")
        return content

    def put_once(self, key, content):
        if key in self.objects and self.objects[key] != content:
            raise ValueError("object collision")
        created = key not in self.objects
        self.objects[key] = content
        self.writes.append(key)
        if self.after_put:
            self.after_put(self, key)
        return created


def populated(store, authority=AUTHORITY):
    registry = ReceiverNameRegistry(store, authority=authority, source_system="ibmi", journal="JRN")
    binding = registry.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    registry.claim(binding, binding.checkpoint(replace(IDENTITY, receiver_name="RCV2", native_generation="native-b"), 1))
    registry.reserve_legacy(JournalPosition("OLD", 99), original_proof_sha256="a" * 64)
    orphan_binding = replace(binding, scope_id="orphan", bootstrap=replace(binding.bootstrap, scope_id="orphan"))
    orphan = orphan_binding.checkpoint(replace(IDENTITY, receiver_name="ORPHAN", native_generation="native-orphan"), 1)
    registry._claim_point(orphan_binding, orphan)
    other = ReceiverNameRegistry(store, authority=authority, source_system="another-source", journal="OTHERJRN")
    other.create_or_load_binding(scope_id="scope-b", identity=replace(IDENTITY, journal_name="OTHERJRN"), sequence=1)
    boundary = JournalBoundary("RLIB", "RCV2", 5, datetime(2026, 10, 13, tzinfo=timezone.utc),
                               replace(IDENTITY, receiver_name="RCV2", native_generation="native-b")).with_binding(binding)
    key = copy_intent_key("pipeline", "table", "run")
    store.put_once(key, encoded({"format_version": "quadringent-copy-intent-v1", "pipeline_id": "pipeline",
                                "table_id": "table", "run_id": "run", "boundary": boundary.to_dict()}))
    return registry, binding, key


def test_complete_authority_roundtrip_preserves_domains_legacy_orphans_and_copy_intents(listing_store):
    authority = AUTHORITY.replace("s3://", "gcs://") if hasattr(listing_store, "bucket_name") else AUTHORITY
    registry, binding, key = populated(listing_store, authority)
    backup = ReceiverAuthorityBackup(listing_store, authority=authority)
    payload = backup.export_inventory(writers_suspended=True)
    objects = backup.validate_inventory(payload, expected_sha256=sha(payload))
    assert set(objects) == set(listing_store.list_keys(100))
    assert key in objects and registry.claim_key("OLD") in objects and registry.claim_key("ORPHAN") in objects
    target = MemoryAuthority()
    restored = ReceiverAuthorityBackup(target, authority=authority)
    restored.preflight_import(payload, expected_sha256=sha(payload), writers_suspended=True)
    assert not target.writes
    restored.import_inventory(payload, expected_sha256=sha(payload), writers_suspended=True)
    restored.verify_inventory(payload, expected_sha256=sha(payload))
    assert target.objects == objects
    restored_registry = ReceiverNameRegistry(target, authority=authority, source_system="ibmi", journal="JRN")
    assert restored_registry.load_binding(binding.scope_id) == binding
    with pytest.raises(ValueError, match="legacy-unbound"):
        restored_registry.claim(binding, binding.checkpoint(replace(IDENTITY, receiver_name="OLD"), 100))
    with pytest.raises(ValueError, match="reuse"):
        restored_registry.claim(binding, binding.checkpoint(replace(IDENTITY, native_generation="new"), 21))


@pytest.mark.parametrize("attestation", [False, None, 1, "true"])
def test_export_and_import_require_explicit_strict_writer_suspension(attestation):
    store = Mock()
    backup = ReceiverAuthorityBackup(store, authority=AUTHORITY)
    with pytest.raises(ValueError, match="suspend"):
        backup.export_inventory(writers_suspended=attestation)
    with pytest.raises(ValueError, match="suspend"):
        backup.import_inventory(b"{}", expected_sha256=sha(b"{}"), writers_suspended=attestation)
    assert not store.mock_calls


@pytest.mark.parametrize("change", ["add", "remove", "replace"])
def test_export_detects_mutation_between_listing_and_read_passes(change):
    store = MemoryAuthority()
    registry, _, _ = populated(store)
    key = registry.claim_key("OLD")
    reads = 0
    def change_after_first_pass(objects, current):
        nonlocal reads
        reads += 1
        if reads == len(objects.objects):
            if change == "add":
                objects.objects["unknown.json"] = b"{}"
            elif change == "remove":
                objects.objects.pop(key)
            else:
                objects.objects[key] += b" "
    store.on_read = change_after_first_pass
    with pytest.raises((ValueError, FileNotFoundError)):
        ReceiverAuthorityBackup(store, authority=AUTHORITY).export_inventory(writers_suspended=True)
    assert len(store.writes) == 8


def test_export_detects_an_object_added_during_the_second_read_pass():
    store = MemoryAuthority()
    populated(store)
    original_count = len(store.objects)
    reads = 0
    def add_during_final_read(objects, key):
        nonlocal reads
        reads += 1
        if reads == original_count * 2:
            objects.objects['late.json'] = b'{}'
    store.on_read = add_during_final_read
    with pytest.raises(ValueError, match='changed'):
        ReceiverAuthorityBackup(store, authority=AUTHORITY).export_inventory(writers_suspended=True)


@pytest.mark.parametrize("damage", ["binding-missing", "bootstrap-claim-missing", "copy-claim-missing",
                                    "unknown-object", "wrong-copy-key", "legacy-copy", "foreign-copy-binding",
                                    "malformed-copy", "duplicate-copy-field", "unknown-copy-version"])
def test_export_refuses_incomplete_or_unknown_authority_objects(damage):
    store = MemoryAuthority()
    registry, binding, intent_key = populated(store)
    intent = json.loads(store.objects[intent_key])
    if damage == "binding-missing":
        store.objects.pop(registry.binding_key(binding.scope_id))
    elif damage == "bootstrap-claim-missing":
        store.objects.pop(registry.claim_key("RCV1"))
    elif damage == "copy-claim-missing":
        store.objects.pop(registry.claim_key("RCV2"))
    elif damage == "unknown-object":
        store.objects["unexpected/raw.json"] = b"{}"
    elif damage == "wrong-copy-key":
        store.objects[copy_intent_key("other", "table", "run")] = store.objects.pop(intent_key)
    else:
        if damage == "legacy-copy":
            intent["boundary"] = {"receiver_library": "RLIB", "receiver_name": "RCV1", "last_sequence": 20,
                                  "observed_at": "2026-10-13T00:00:00+00:00"}
        elif damage == "foreign-copy-binding":
            intent["boundary"]["capture_binding"]["capture_epoch"] = "foreign"
            intent["boundary"]["capture_binding"]["bootstrap"]["capture_epoch"] = "foreign"
            intent["boundary"]["bound_checkpoint"]["capture_epoch"] = "foreign"
        elif damage == "malformed-copy":
            intent["pipeline_id"] = True
        elif damage == "unknown-copy-version":
            intent["format_version"] = "unknown"
        content = encoded(intent)
        if damage == "duplicate-copy-field":
            content = content.rstrip()[:-1] + b',"run_id":"run"}'
        store.objects[intent_key] = content
    with pytest.raises(ValueError):
        ReceiverAuthorityBackup(store, authority=AUTHORITY).export_inventory(writers_suspended=True)


def test_orphan_claims_are_preserved_without_inventing_missing_admissions():
    store = MemoryAuthority()
    registry, binding, intent_key = populated(store)
    store.objects.pop(intent_key)
    store.objects.pop(registry.binding_key(binding.scope_id))
    payload = ReceiverAuthorityBackup(store, authority=AUTHORITY).export_inventory(writers_suspended=True)
    assert registry.claim_key("RCV1") in ReceiverAuthorityBackup(store, authority=AUTHORITY).validate_inventory(
        payload, expected_sha256=sha(payload))


@pytest.mark.parametrize("damage", ["payload-hash", "object-hash", "duplicate-key", "extra-field", "version",
                                    "authority", "unknown-object", "missing-bootstrap-claim"])
def test_import_validates_the_entire_manifest_before_any_io_or_write(damage):
    source = MemoryAuthority()
    registry, _, _ = populated(source)
    payload = ReceiverAuthorityBackup(source, authority=AUTHORITY).export_inventory(writers_suspended=True)
    record = json.loads(payload)
    if damage == "object-hash":
        record["objects"][-1]["sha256"] = "0" * 64
    elif damage == "duplicate-key":
        record["objects"].append(deepcopy(record["objects"][0]))
    elif damage == "extra-field":
        record["extra"] = True
    elif damage == "version":
        record["format_version"] = "future"
    elif damage == "authority":
        record["registry_authority"] = "s3://another-bucket/receiver-authority-v1"
    elif damage == "unknown-object":
        record["objects"].append({"key": "unknown.json", "sha256": sha(b"{}"), "content": "{}"})
    elif damage == "missing-bootstrap-claim":
        record["objects"] = [entry for entry in record["objects"] if entry["key"] != registry.claim_key("RCV1")]
    payload = encoded(record)
    target = Mock()
    with pytest.raises(ValueError):
        ReceiverAuthorityBackup(target, authority=AUTHORITY).import_inventory(
            payload, expected_sha256="0" * 64 if damage == "payload-hash" else sha(payload), writers_suspended=True)
    assert not target.mock_calls


@pytest.mark.parametrize("damage", ["collision", "extra"])
def test_import_preflights_all_target_collisions_before_restoring_any_object(damage):
    source = MemoryAuthority()
    populated(source)
    payload = ReceiverAuthorityBackup(source, authority=AUTHORITY).export_inventory(writers_suspended=True)
    key = sorted(source.objects)[-1]
    target = MemoryAuthority({key: b"rival"} if damage == "collision" else {"unknown.json": b"rival"})
    before = dict(target.objects)
    with pytest.raises(ValueError):
        ReceiverAuthorityBackup(target, authority=AUTHORITY).import_inventory(
            payload, expected_sha256=sha(payload), writers_suspended=True)
    assert target.objects == before and not target.writes


@pytest.mark.parametrize("damage", ["absent", "extra", "changed"])
def test_verify_requires_exact_complete_listing_and_original_bytes(damage):
    store = MemoryAuthority()
    populated(store)
    backup = ReceiverAuthorityBackup(store, authority=AUTHORITY)
    payload = backup.export_inventory(writers_suspended=True)
    key = sorted(store.objects)[0]
    if damage == "absent":
        store.objects.pop(key)
    elif damage == "extra":
        store.objects["new.json"] = b"{}"
    else:
        store.objects[key] += b" "
    before = len(store.writes)
    with pytest.raises(ValueError):
        backup.verify_inventory(payload, expected_sha256=sha(payload))
    assert len(store.writes) == before


def test_import_never_reports_success_when_a_rival_object_appears_during_restore():
    source = MemoryAuthority()
    populated(source)
    payload = ReceiverAuthorityBackup(source, authority=AUTHORITY).export_inventory(writers_suspended=True)
    target = MemoryAuthority()
    target.after_put = lambda objects, key: objects.objects.setdefault("unexpected.json", b"rival")
    with pytest.raises(ValueError):
        ReceiverAuthorityBackup(target, authority=AUTHORITY).import_inventory(
            payload, expected_sha256=sha(payload), writers_suspended=True)
    assert target.objects["unexpected.json"] == b"rival"


def test_inventory_limits_reject_overflow_instead_of_truncating():
    store = MemoryAuthority()
    populated(store)
    backup = ReceiverAuthorityBackup(store, authority=AUTHORITY)
    with pytest.raises(ValueError, match="budget"):
        backup.export_inventory(writers_suspended=True, max_keys=1)
    with pytest.raises(ValueError, match="budget"):
        backup.export_inventory(writers_suspended=True, max_bytes=100)
    for budget in (True, 0, -1, "100"):
        with pytest.raises(ValueError):
            backup.export_inventory(writers_suspended=True, max_keys=budget)


def test_empty_authority_is_explicit_and_roundtrips_without_objects():
    store = MemoryAuthority()
    backup = ReceiverAuthorityBackup(store, authority=AUTHORITY)
    payload = backup.export_inventory(writers_suspended=True)
    assert backup.validate_inventory(payload, expected_sha256=sha(payload)) == {}
    backup.import_inventory(payload, expected_sha256=sha(payload), writers_suspended=True)
    assert not store.writes


def test_restore_failure_preserves_partial_original_bytes_and_can_resume():
    source = MemoryAuthority()
    populated(source)
    payload = ReceiverAuthorityBackup(source, authority=AUTHORITY).export_inventory(writers_suspended=True)
    target = MemoryAuthority()
    def fail_after_second_write(objects, key):
        if len(objects.writes) == 2:
            raise RuntimeError('simulated crash')
    target.after_put = fail_after_second_write
    backup = ReceiverAuthorityBackup(target, authority=AUTHORITY)
    with pytest.raises(RuntimeError, match='simulated crash'):
        backup.import_inventory(payload, expected_sha256=sha(payload), writers_suspended=True)
    assert len(target.objects) == 2
    assert all(content == source.objects[key] for key, content in target.objects.items())
    target.after_put = None
    backup.import_inventory(payload, expected_sha256=sha(payload), writers_suspended=True)
    assert target.objects == source.objects


def test_corrupt_historical_claim_is_not_silently_omitted():
    store = MemoryAuthority()
    registry, _, _ = populated(store)
    key = registry.claim_key('OLD')
    claim = json.loads(store.objects[key])
    claim['original_checkpoint']['sequence'] = True
    store.objects[key] = encoded(claim)
    with pytest.raises(ValueError, match='legacy'):
        ReceiverAuthorityBackup(store, authority=AUTHORITY).export_inventory(writers_suspended=True)


@pytest.mark.parametrize('authority', [None, '', 's3://registry-bucket/other',
                                     'gcs://registry-bucket/receiver-authority-v1/',
                                     'S3://registry-bucket/receiver-authority-v1'])
def test_noncanonical_authority_is_refused_before_storage_access(authority):
    store = Mock()
    with pytest.raises(ValueError):
        ReceiverAuthorityBackup(store, authority=authority)
    assert not store.mock_calls
