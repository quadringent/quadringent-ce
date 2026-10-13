from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from threading import Barrier, Lock
from uuid import UUID

import pytest

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.gcs_backend import GcsObjectStore
from quadringent.object_store import FileObjectStore, S3ObjectStore
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from test_gcs_backend import FakeGcsClient


AUTHORITY = "s3://registry-bucket/receiver-authority-v1"
IDENTITY = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "native-test-v1", "generation-a")


class AtomicObjects:
    def __init__(self):
        self.objects = {}
        self.lock = Lock()
        self.before_put = None
        self.after_put = None

    def get_bounded(self, key, max_bytes):
        with self.lock:
            if key not in self.objects:
                raise FileNotFoundError(key)
            content = self.objects[key]
        if len(content) > max_bytes:
            raise ValueError("object exceeds read budget")
        return content

    def put_once(self, key, content):
        if self.before_put is not None:
            self.before_put(key)
        with self.lock:
            if key in self.objects:
                if self.objects[key] != content:
                    raise ValueError("object collision")
                return False
            self.objects[key] = bytes(content)
        if self.after_put is not None:
            self.after_put(key)
        return True


class AtomicS3Client:
    def __init__(self):
        self.objects = {}
        self.lock = Lock()

    def _missing(self):
        error = RuntimeError("not found")
        error.response = {"Error": {"Code": "404"}}
        return error

    def head_object(self, *, Bucket, Key):
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise self._missing()
            return {"Metadata": deepcopy(self.objects[(Bucket, Key)][1])}

    def get_object(self, *, Bucket, Key):
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise self._missing()
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch):
        assert IfNoneMatch == "*"
        with self.lock:
            if (Bucket, Key) in self.objects:
                error = RuntimeError("precondition failed")
                error.response = {"Error": {"Code": "412"}}
                raise error
            self.objects[(Bucket, Key)] = (bytes(Body), deepcopy(Metadata))


def registry(store, **changes):
    return ReceiverNameRegistry(store, authority=changes.get("authority", getattr(store, "registry_authority", AUTHORITY)),
                                source_system=changes.get("source_system", "ibmi"),
                                journal=changes.get("journal", "JRN"))


@pytest.fixture(params=["file", "s3", "gcs"])
def durable_store(request, tmp_path):
    if request.param == "file":
        return FileObjectStore(tmp_path / "authority")
    if request.param == "s3":
        return S3ObjectStore("registry-bucket", "receiver-authority-v1", client=AtomicS3Client())
    store = GcsObjectStore("registry-bucket", "receiver-authority-v1", client=FakeGcsClient())
    store.registry_authority = "gcs://registry-bucket/receiver-authority-v1"
    return store


def test_binding_is_created_once_and_survives_registry_recreation(durable_store):
    first = registry(durable_store).create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    assert str(UUID(first.capture_epoch)) == first.capture_epoch
    restarted = registry(durable_store)
    assert restarted.load_binding("scope-a") == first
    later = restarted.create_or_load_binding(scope_id="scope-a", identity=replace(IDENTITY, receiver_name="RCV2"), sequence=99)
    assert later == first
    restarted.require(first, first.bootstrap)


def test_different_scopes_share_name_claims_without_replacing_original_provenance():
    store = AtomicObjects()
    names = registry(store)
    first = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    original = store.objects[names.claim_key("RCV1")]
    second = names.create_or_load_binding(scope_id="scope-b", identity=IDENTITY, sequence=30)
    assert second.capture_epoch != first.capture_epoch
    names.claim(second, second.bootstrap)
    names.require(second, second.bootstrap)
    assert store.objects[names.claim_key("RCV1")] == original
    assert json.loads(original)["binding"] == first.to_dict()


@pytest.mark.parametrize("changes", [{"native_generation": "generation-new"}, {"receiver_library": "OTHER"},
                                    {"native_generation_kind": "native-other-v1"}])
def test_rotation_back_to_a_used_name_cannot_acquire_another_identity(durable_store, changes):
    names = registry(durable_store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    successor = replace(IDENTITY, receiver_name="RCV2", native_generation="generation-b")
    names.claim(binding, binding.checkpoint(successor, 1))
    restarted = registry(durable_store)
    before = durable_store.get_bounded(names.claim_key("RCV1"), 65536)
    with pytest.raises(ValueError, match="reus|identity"):
        restarted.claim(binding, binding.checkpoint(replace(IDENTITY, **changes), 1))
    with pytest.raises(ValueError, match="reus|identity"):
        restarted.create_or_load_binding(scope_id="new-scope", identity=replace(IDENTITY, **changes), sequence=1)
    assert durable_store.get_bounded(names.claim_key("RCV1"), 65536) == before


def test_claim_survives_a_later_checkpoint_failure_and_exact_retry():
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    successor = binding.checkpoint(replace(IDENTITY, receiver_name="RCV2", native_generation="generation-b"), 1)
    names.claim(binding, successor)
    original = store.objects[names.claim_key("RCV2")]
    with pytest.raises(RuntimeError, match="CAS"):
        raise RuntimeError("simulated CAS failure")
    registry(store).claim(binding, successor)
    registry(store).require(binding, successor)
    assert store.objects[names.claim_key("RCV2")] == original


def test_crash_between_original_claim_and_binding_keeps_the_orphan_reservation():
    store = AtomicObjects()
    names = registry(store)

    def crash(key):
        if "/names/" in key:
            raise RuntimeError("simulated crash after durable claim")

    store.after_put = crash
    with pytest.raises(RuntimeError, match="durable claim"):
        names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    original = store.objects[names.claim_key("RCV1")]
    assert names.load_binding("scope-a") is None
    store.after_put = None
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    names.require(binding, binding.bootstrap)
    assert store.objects[names.claim_key("RCV1")] == original


def test_two_first_writers_keep_one_original_binding_and_claim():
    store = AtomicObjects()
    barrier = Barrier(2)
    store.before_put = lambda key: barrier.wait(timeout=5) if "/names/" in key else None

    def start(sequence):
        return registry(store).create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=sequence)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(start, [20, 30]))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].bootstrap.sequence in {20, 30}
    assert len([key for key in store.objects if "/names/" in key]) == 1


def test_two_native_generations_racing_for_one_name_have_one_winner():
    store = AtomicObjects()
    barrier = Barrier(2)
    store.before_put = lambda key: barrier.wait(timeout=5) if "/names/" in key else None

    def start(generation):
        try:
            registry(store).create_or_load_binding(scope_id="scope-" + generation,
                                                  identity=replace(IDENTITY, native_generation=generation), sequence=20)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start, ["one", "two"])) == [False, True]
    assert len([key for key in store.objects if "/bindings/" in key]) == 1


@pytest.mark.parametrize("missing", ["binding", "bootstrap", "current"])
def test_require_refuses_a_missing_expected_record_without_recreating_it(missing):
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    point = binding.checkpoint(replace(IDENTITY, receiver_name="RCV2", native_generation="generation-b"), 1)
    names.claim(binding, point)
    key = {"binding": names.binding_key("scope-a"), "bootstrap": names.claim_key("RCV1"),
           "current": names.claim_key("RCV2")}[missing]
    del store.objects[key]
    before = deepcopy(store.objects)
    with pytest.raises(ValueError, match="absent|missing"):
        names.require(binding, point)
    assert store.objects == before


def test_existing_binding_with_missing_original_claim_is_not_silently_repaired():
    store = AtomicObjects()
    names = registry(store)
    names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    del store.objects[names.claim_key("RCV1")]
    before = deepcopy(store.objects)
    with pytest.raises(ValueError, match="absent|missing"):
        names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    assert store.objects == before


@pytest.mark.parametrize("record_kind", ["binding", "claim"])
@pytest.mark.parametrize("corruption", ["version", "extra", "domain", "duplicate", "identity"])
def test_corrupt_records_are_rejected_without_writes(record_kind, corruption):
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    key = names.binding_key("scope-a") if record_kind == "binding" else names.claim_key("RCV1")
    record = json.loads(store.objects[key])
    if corruption == "version":
        record["format_version"] = "future"
    elif corruption == "extra":
        record["unexpected"] = "cannot-drop"
    elif corruption == "domain":
        record["source_system"] = "other"
    elif corruption == "identity":
        target = record["bootstrap"] if record_kind == "binding" else record["first_checkpoint"]
        target["receiver_identity"]["receiver_name"] = "OTHER"
    payload = json.dumps(record).encode()
    if corruption == "duplicate":
        payload = payload[:-1] + b',"format_version":"duplicate"}'
    store.objects[key] = payload
    before = deepcopy(store.objects)
    with pytest.raises(ValueError):
        names.require(binding, binding.bootstrap)
    assert store.objects == before


def test_another_authority_or_event_domain_cannot_use_the_saved_binding():
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    for changes in ({"authority": "gcs://other-bucket/receiver-authority-v1"}, {"source_system": "other"}, {"journal": "OTHER"}):
        with pytest.raises(ValueError):
            registry(store, **changes).require(binding, binding.bootstrap)


def test_legacy_name_is_reserved_without_fabricating_a_native_binding():
    store = AtomicObjects()
    names = registry(store)
    names.reserve_legacy(JournalPosition("RCV1", 10), original_proof_sha256="a" * 64)
    original = store.objects[names.claim_key("RCV1")]
    assert json.loads(original)["state"] == "legacy-unbound"
    assert "receiver_identity" not in original.decode()
    with pytest.raises(ValueError, match="legacy|unbound"):
        names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    assert names.load_binding("scope-a") is None
    assert store.objects[names.claim_key("RCV1")] == original


def test_full_inventory_restores_bindings_retired_names_and_orphan_claims():
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    point = binding.checkpoint(replace(IDENTITY, receiver_name="RCV2", native_generation="generation-b"), 1)
    names.claim(binding, point)
    names.reserve_legacy(JournalPosition("OLD", 10), original_proof_sha256="a" * 64)

    def interrupt_binding_creation(key):
        if key == names.claim_key("ORPHAN"):
            raise RuntimeError("crash after orphan claim")

    store.after_put = interrupt_binding_creation
    with pytest.raises(RuntimeError, match="orphan"):
        names.create_or_load_binding(scope_id="orphan-scope", identity=replace(IDENTITY, receiver_name="ORPHAN"), sequence=1)
    store.after_put = None
    inventory = names.export_inventory(tuple(store.objects))
    digest = hashlib.sha256(inventory).hexdigest()
    restored_store = AtomicObjects()
    restored = registry(restored_store)
    restored.import_inventory(inventory, expected_sha256=digest)
    restored.verify_inventory(inventory, expected_sha256=digest)
    assert restored_store.objects == store.objects
    restored.require(binding, point)
    with pytest.raises(ValueError, match="reus|identity"):
        restored.claim(binding, binding.checkpoint(replace(IDENTITY, native_generation="new"), 1))
    with pytest.raises(ValueError, match="reus|identity"):
        restored.create_or_load_binding(scope_id="orphan-retry", identity=replace(IDENTITY, receiver_name="ORPHAN",
                                                                               native_generation="new"), sequence=1)
    del restored_store.objects[restored.claim_key("RCV1")]
    with pytest.raises(ValueError, match="absent|missing"):
        restored.verify_inventory(inventory, expected_sha256=digest)


def test_inventory_digest_domain_and_bounds_are_checked_before_restore_writes():
    store = AtomicObjects()
    names = registry(store)
    names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    inventory = names.export_inventory(tuple(store.objects))
    target = AtomicObjects()
    with pytest.raises(ValueError):
        registry(target).import_inventory(inventory, expected_sha256="b" * 64)
    with pytest.raises(ValueError):
        registry(target, authority="gcs://other-bucket/receiver-authority-v1").import_inventory(
            inventory, expected_sha256=hashlib.sha256(inventory).hexdigest())
    with pytest.raises(ValueError, match="budget|limit"):
        names.export_inventory(tuple(store.objects), max_keys=1)
    assert target.objects == {}


def test_receiver_registry_does_not_change_historical_event_ids():
    event = ChangeEvent("ibmi", "JRN", "LIB", "TABLE", "c", JournalPosition("RCV1", 20),
                        "2026-10-13T00:00:00Z", "as400-raw-v1", None, {"ID": 1})
    before = event.event_id
    registry(AtomicObjects()).create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    assert event.event_id == before == hashlib.sha256(b"ibmi|JRN|RCV1|20").hexdigest()


@pytest.mark.parametrize("operation", ["claim", "require"])
def test_registry_operations_reject_non_binding_objects(operation):
    names = registry(AtomicObjects())
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    with pytest.raises(ValueError):
        getattr(names, operation)(None, binding.bootstrap)


def test_scope_epoch_or_bootstrap_cannot_be_replaced_in_memory():
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    forged = replace(binding, capture_epoch="another", bootstrap=replace(binding.bootstrap, capture_epoch="another"))
    before = deepcopy(store.objects)
    with pytest.raises(ValueError, match="original"):
        names.claim(forged, forged.bootstrap)
    assert store.objects == before


@pytest.mark.parametrize("corruption", ["unknown", "duplicate-key", "object-digest", "missing-bootstrap", "external-key"])
def test_malformed_inventory_is_rejected_before_any_restore_write(corruption):
    store = AtomicObjects()
    names = registry(store)
    binding = names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    record = json.loads(names.export_inventory(tuple(store.objects)))
    if corruption == "unknown":
        record["unexpected"] = "cannot-drop"
    elif corruption == "duplicate-key":
        record["objects"].append(deepcopy(record["objects"][0]))
    elif corruption == "object-digest":
        record["objects"][0]["sha256"] = "b" * 64
    elif corruption == "missing-bootstrap":
        record["objects"] = [entry for entry in record["objects"]
                             if entry["key"] != names.claim_key(binding.bootstrap.position.receiver)]
    else:
        record["objects"][0]["key"] = "outside-domain.json"
    payload = json.dumps(record).encode()
    target = AtomicObjects()
    with pytest.raises(ValueError):
        registry(target).import_inventory(payload, expected_sha256=hashlib.sha256(payload).hexdigest())
    assert target.objects == {}


def test_restore_rejects_an_existing_conflicting_claim_without_overwriting_it():
    source = AtomicObjects()
    names = registry(source)
    names.create_or_load_binding(scope_id="scope-a", identity=IDENTITY, sequence=20)
    inventory = names.export_inventory(tuple(source.objects))
    target = AtomicObjects()
    registry(target).create_or_load_binding(scope_id="other-scope", identity=replace(IDENTITY, native_generation="new"), sequence=1)
    original = target.objects[names.claim_key("RCV1")]
    with pytest.raises(ValueError, match="collision"):
        registry(target).import_inventory(inventory, expected_sha256=hashlib.sha256(inventory).hexdigest())
    assert target.objects[names.claim_key("RCV1")] == original
