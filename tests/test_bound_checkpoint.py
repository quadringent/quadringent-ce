from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import re
from threading import Barrier, Lock

import pytest

from quadringent.checkpoint import DynamoDbCheckpointStore, JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.gcs_backend import GcsCheckpointStore
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from test_gcs_backend import FakeGcsClient


IDENTITY = ReceiverIdentity("JLIB", "JRN", "RLIB", "R1", "native-test-v1", "opaque-generation-a")
BOUND = BoundCheckpoint("scope-a", "epoch-a", IDENTITY, 20)


class AtomicDynamoClient:
    """Evaluate the CAS attribute guards used by checkpoint writers offline."""

    def __init__(self):
        self.item = None
        self.put_calls = []
        self.get_calls = []
        self.before_write = None
        self.lock = Lock()

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        with self.lock:
            return {} if self.item is None else {"Item": deepcopy(self.item)}

    def put_item(self, **kwargs):
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        with self.lock:
            self.put_calls.append(deepcopy(kwargs))
            expression = kwargs["ConditionExpression"]
            names = kwargs["ExpressionAttributeNames"]
            values = kwargs.get("ExpressionAttributeValues", {})
            conflict = False
            for alias in re.findall(r"attribute_not_exists\((#[a-z_]+)\)", expression):
                if alias == "#format" and "OR" in expression:
                    continue
                conflict |= self.item is not None and names[alias] in self.item
            for alias, token in re.findall(r"(#[a-z_]+) = (:[a-z_]+)", expression):
                actual = None if self.item is None else self.item.get(names[alias])
                if alias == "#format" and "OR" in expression and actual is None:
                    continue
                conflict |= actual != values[token]
            if conflict:
                error = RuntimeError("conditional write failed")
                error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
                raise error
            self.item = deepcopy(kwargs["Item"])


class StoreHarness:
    def __init__(self, backend, tmp_path):
        self.backend = backend
        self.path = tmp_path / "checkpoint.json"
        self.client = AtomicDynamoClient() if backend == "dynamo" else FakeGcsClient()

    def store(self):
        if self.backend == "json":
            return JsonCheckpointStore(self.path)
        if self.backend == "dynamo":
            return DynamoDbCheckpointStore("checkpoints", "stream-a", client=self.client)
        return GcsCheckpointStore("sandbox-state", "stream-a", client=self.client)

    def durable_bytes(self):
        if self.backend == "json":
            return self.path.read_bytes()
        if self.backend == "dynamo":
            return json.dumps(self.client.item, sort_keys=True).encode()
        return self.client.objects[("sandbox-state", self.store().object_name)][0]

    def set_record(self, record):
        if self.backend == "dynamo":
            self.client.item = {"pk": {"S": "stream-a"}, **deepcopy(record)}
        elif self.backend == "json":
            self.path.write_bytes(json.dumps(record).encode())
        else:
            self.client._generation += 1
            self.client.objects[("sandbox-state", self.store().object_name)] = (
                json.dumps({"stream_id": "stream-a", **record}).encode(), {}, self.client._generation,
            )


@pytest.fixture(params=["json", "dynamo", "gcs"])
def harness(request, tmp_path):
    return StoreHarness(request.param, tmp_path)


def test_bound_models_round_trip_and_preserve_the_historical_position():
    record = BOUND.to_dict()
    assert record["format_version"] == "as400-checkpoint-v2"
    assert record["receiver_identity"]["format_version"] == "as400-receiver-identity-v1"
    assert BoundCheckpoint.from_dict(record) == BOUND
    assert ReceiverIdentity.from_dict(IDENTITY.to_dict()) == IDENTITY
    assert BOUND.position == JournalPosition("R1", 20)
    assert IDENTITY.native_generation == "opaque-generation-a"


@pytest.mark.parametrize("field", ["journal_library", "journal_name", "receiver_library", "receiver_name",
                                   "native_generation_kind", "native_generation"])
@pytest.mark.parametrize("bad", [None, True, 7, {}, "", " ", "x\n"])
def test_receiver_identity_rejects_invalid_field_types_and_empty_values(field, bad):
    with pytest.raises(ValueError):
        replace(IDENTITY, **{field: bad})


@pytest.mark.parametrize("field", ["journal_library", "journal_name", "receiver_library", "receiver_name"])
@pytest.mark.parametrize("bad", ["rcv", "Rcv", "WITH SPACE", "R.CV", "R/CV", "R:CV", "R１", "R" * 11])
def test_ibm_system_names_must_be_canonical_uppercase_ascii(field, bad):
    with pytest.raises(ValueError, match="canonical|system name"):
        replace(IDENTITY, **{field: bad})


def test_canonical_system_names_and_opaque_generation_are_preserved_exactly():
    identity = ReceiverIdentity("$JLIB", "JRN_1", "#RLIB", "@RCV9", "Native-Api/v1", "gEnErAtIoN:opaque-value")
    assert ReceiverIdentity.from_dict(identity.to_dict()) == identity
    assert identity.native_generation == "gEnErAtIoN:opaque-value"
    assert identity.native_generation_kind == "Native-Api/v1"


@pytest.mark.parametrize("changes", [
    {"scope_id": True}, {"scope_id": ""}, {"capture_epoch": 1}, {"capture_epoch": " "},
    {"receiver_identity": IDENTITY.to_dict()}, {"sequence": True}, {"sequence": "20"},
    {"sequence": 20.0}, {"sequence": -1},
])
def test_bound_checkpoint_rejects_invalid_types(changes):
    with pytest.raises(ValueError):
        replace(BOUND, **changes)


@pytest.mark.parametrize("model", [BOUND, IDENTITY])
@pytest.mark.parametrize("corruption", ["version", "missing", "extra", "not-object"])
def test_models_reject_unknown_versions_missing_and_extra_fields(model, corruption):
    record = model.to_dict()
    if corruption == "version":
        record["format_version"] = "future-version"
    elif corruption == "missing":
        del record[next(key for key in record if key != "format_version")]
    elif corruption == "extra":
        record["unexpected"] = "cannot-drop"
    else:
        record = []
    with pytest.raises(ValueError):
        type(model).from_dict(record)


@pytest.mark.parametrize("model", [BOUND, IDENTITY])
@pytest.mark.parametrize("version", [None, True, 1, {}, []])
def test_model_format_versions_require_a_string(model, version):
    record = model.to_dict()
    record["format_version"] = version
    with pytest.raises(ValueError):
        type(model).from_dict(record)


def test_bound_checkpoint_survives_store_recreation_and_load_projection(harness):
    store = harness.store()
    assert store.load_bound() is None
    store.compare_and_set_bound(None, BOUND)
    restarted = harness.store()
    assert restarted.load_bound() == BOUND
    assert restarted.load() == BOUND.position
    restarted.compare_and_set_bound(BOUND, replace(BOUND, sequence=30))
    assert store.load_bound().sequence == 30


@pytest.mark.parametrize("change", ["generation", "generation-kind", "receiver-library", "scope", "epoch"])
def test_same_position_with_a_different_binding_cannot_satisfy_cas(harness, change):
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    if change == "scope":
        wrong = replace(BOUND, scope_id="scope-b")
    elif change == "epoch":
        wrong = replace(BOUND, capture_epoch="epoch-b")
    else:
        field, value = {
            "generation": ("native_generation", "opaque-generation-b"),
            "generation-kind": ("native_generation_kind", "other-native-kind"),
            "receiver-library": ("receiver_library", "OTHER"),
        }[change]
        wrong = replace(BOUND, receiver_identity=replace(IDENTITY, **{field: value}))
    before = harness.durable_bytes()
    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        store.compare_and_set_bound(wrong, replace(wrong, sequence=30))
    assert harness.durable_bytes() == before


@pytest.mark.parametrize("changes", [
    {"scope_id": "scope-b"}, {"capture_epoch": "epoch-b"}, {"sequence": 19},
    {"receiver_identity": replace(IDENTITY, journal_library="OTHER")},
    {"receiver_identity": replace(IDENTITY, journal_name="OTHER")},
    {"receiver_identity": replace(IDENTITY, receiver_name="R2", journal_library="OTHER")},
    {"receiver_identity": replace(IDENTITY, native_generation="opaque-generation-b")},
    {"receiver_identity": replace(IDENTITY, receiver_library="OTHER")},
])
def test_bound_updates_cannot_change_binding_or_regress_same_receiver(harness, changes):
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    before = harness.durable_bytes()
    with pytest.raises(ValueError):
        store.compare_and_set_bound(BOUND, replace(BOUND, **changes))
    assert harness.durable_bytes() == before


def test_same_native_name_with_a_case_alias_and_new_generation_never_rotates_or_writes(harness):
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    before = harness.durable_bytes()
    with pytest.raises(ValueError, match="canonical|system name"):
        alias = replace(IDENTITY, receiver_name="r1", native_generation="opaque-generation-b")
        store.compare_and_set_bound(BOUND, replace(BOUND, receiver_identity=alias, sequence=1))
    assert harness.durable_bytes() == before
    assert store.load_bound() == BOUND


def test_explicit_bound_receiver_rotation_allows_a_new_generation_and_reset(harness):
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    successor = replace(BOUND, receiver_identity=replace(IDENTITY,
        receiver_name="R2", native_generation="opaque-generation-b"), sequence=1)
    store.compare_and_set_bound(BOUND, successor)
    assert store.load_bound() == successor


def test_two_bound_writers_cannot_both_replace_the_same_predecessor(harness):
    harness.store().compare_and_set_bound(None, BOUND)
    barrier = Barrier(2)

    def attempt(sequence):
        barrier.wait(timeout=5)
        try:
            harness.store().compare_and_set_bound(BOUND, replace(BOUND, sequence=sequence))
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [30, 40]))
    assert results.count(True) == 1
    assert harness.store().load_bound().sequence == (30 if results[0] else 40)


def test_legacy_v1_is_inspectable_but_never_automatically_bound(harness):
    store = harness.store()
    store.commit(BOUND.position)
    before = harness.durable_bytes()
    assert store.load() == BOUND.position
    with pytest.raises(ValueError, match="legacy|v1|unbound"):
        store.load_bound()
    for previous in (None, BOUND):
        with pytest.raises(ValueError, match="legacy|v1|unbound"):
            store.compare_and_set_bound(previous, BOUND)
        assert harness.durable_bytes() == before


@pytest.mark.parametrize("operation", ["commit", "transition", "compare-and-set", "initial-cas"])
def test_legacy_writers_never_downgrade_a_bound_checkpoint(harness, operation):
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    before = harness.durable_bytes()
    with pytest.raises(ValueError, match="bound|v2|legacy"):
        if operation == "commit":
            store.commit(JournalPosition("R1", 30))
        elif operation == "transition":
            store.transition(BOUND.position, JournalPosition("R2", 1))
        else:
            store.compare_and_set(None if operation == "initial-cas" else BOUND.position,
                                  JournalPosition("R1", 30))
    assert harness.durable_bytes() == before


@pytest.mark.parametrize("corruption", ["version", "boolean-sequence", "extra-field", "missing-sequence"])
def test_stores_reject_corrupt_records_for_both_read_paths_and_preserve_bytes(harness, corruption):
    if harness.backend == "dynamo":
        record = {"format_version": {"S": "as400-checkpoint-v1"},
                  "receiver": {"S": "R1"}, "sequence": {"N": "20"}}
        if corruption == "version": record["format_version"] = {"S": "future-checkpoint"}
        elif corruption == "boolean-sequence": record["sequence"] = {"BOOL": True}
        elif corruption == "extra-field": record["unexpected"] = {"S": "cannot-drop"}
        else: del record["sequence"]
    else:
        record = {"format_version": "as400-checkpoint-v1", "receiver": "R1", "sequence": 20}
        if corruption == "version": record["format_version"] = "future-checkpoint"
        elif corruption == "boolean-sequence": record["sequence"] = True
        elif corruption == "extra-field": record["unexpected"] = "cannot-drop"
        else: del record["sequence"]
    harness.set_record(record)
    before = harness.durable_bytes()
    for operation in (harness.store().load, harness.store().load_bound,
                      lambda: harness.store().compare_and_set_bound(None, BOUND)):
        with pytest.raises(ValueError):
            operation()
        assert harness.durable_bytes() == before


def test_dynamo_legacy_cas_guard_blocks_a_bound_writer_after_the_legacy_read(tmp_path):
    harness = StoreHarness("dynamo", tmp_path)
    store = harness.store()
    store.commit(BOUND.position)
    other = AtomicDynamoClient()
    DynamoDbCheckpointStore("checkpoints", "stream-a", client=other).compare_and_set_bound(None, BOUND)
    bound_item = deepcopy(other.item)
    harness.client.before_write = lambda: setattr(harness.client, "item", bound_item)

    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        store.compare_and_set(BOUND.position, JournalPosition("R1", 30))

    assert harness.client.item == bound_item
    call = harness.client.put_calls[-1]
    assert call["ExpressionAttributeNames"]["#format"] == "format_version"
    assert ":legacy_format" in call["ExpressionAttributeValues"]


def test_dynamo_bound_cas_guards_the_complete_envelope_and_keeps_existing_key(tmp_path):
    harness = StoreHarness("dynamo", tmp_path)
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    store.compare_and_set_bound(BOUND, replace(BOUND, sequence=30))
    call = harness.client.put_calls[-1]
    names = call["ExpressionAttributeNames"]
    assert {"checkpoint_digest", "format_version", "scope_id", "capture_epoch", "receiver_identity"} <= set(names.values())
    assert all(alias in call["ConditionExpression"] for alias in names)
    assert harness.client.get_calls[-1]["Key"] == {"pk": {"S": "stream-a"}}
    assert harness.client.get_calls[-1]["ConsistentRead"] is True


def test_gcs_bound_cas_guards_a_replacement_between_read_and_write(tmp_path):
    harness = StoreHarness("gcs", tmp_path)
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    successor = replace(BOUND, sequence=40)
    harness.client.before_write = lambda: harness.store().compare_and_set_bound(BOUND, successor)
    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        store.compare_and_set_bound(BOUND, replace(BOUND, sequence=30))
    assert store.load_bound() == successor
    assert harness.client.writes[-1][2] == harness.client.writes[-2][2]
    assert harness.client.objects[("sandbox-state", store.object_name)][2] > harness.client.writes[-1][2]


def test_gcs_legacy_writer_cannot_overwrite_v2_created_after_its_read(tmp_path):
    harness = StoreHarness("gcs", tmp_path)
    store = harness.store()
    store.commit(BOUND.position)
    other = StoreHarness("gcs", tmp_path / "other")
    other.store().compare_and_set_bound(None, BOUND)

    def replace_with_bound():
        harness.client._generation += 1
        harness.client.objects[("sandbox-state", store.object_name)] = (
            other.durable_bytes(), {}, harness.client._generation,
        )

    harness.client.before_write = replace_with_bound
    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        store.compare_and_set(BOUND.position, JournalPosition("R1", 30))
    assert store.load_bound() == BOUND


def test_dynamo_unversioned_legacy_item_remains_inspectable_without_binding(tmp_path):
    harness = StoreHarness("dynamo", tmp_path)
    harness.set_record({"receiver": {"S": "R1"}, "sequence": {"N": "20"}})
    assert harness.store().load() == BOUND.position
    with pytest.raises(ValueError, match="legacy|v1|unbound"):
        harness.store().load_bound()


@pytest.mark.parametrize("backend", ["dynamo", "gcs"])
@pytest.mark.parametrize("change", ["generation", "scope", "epoch"])
def test_bound_cas_blocks_same_position_replaced_with_another_binding_after_read(backend, change, tmp_path):
    harness = StoreHarness(backend, tmp_path)
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    if change == "generation":
        replacement = replace(BOUND, receiver_identity=replace(IDENTITY, native_generation="generation-b"))
    else:
        replacement = replace(BOUND, **{"scope_id" if change == "scope" else "capture_epoch": "other"})
    other = StoreHarness(backend, tmp_path / "other")
    other.store().compare_and_set_bound(None, replacement)

    def replace_after_read():
        if backend == "dynamo":
            harness.client.item = deepcopy(other.client.item)
        else:
            key = ("sandbox-state", store.object_name)
            harness.client._generation += 1
            harness.client.objects[key] = (other.durable_bytes(), {}, harness.client._generation)

    harness.client.before_write = replace_after_read
    with pytest.raises(RuntimeError, match="compare-and-set conflict"):
        store.compare_and_set_bound(BOUND, replace(BOUND, sequence=30))
    assert store.load_bound() == replacement


@pytest.mark.parametrize("corruption", ["identity", "scope", "sequence", "digest", "identity-type"])
def test_dynamo_bound_record_rejects_digest_mismatch_or_invalid_native_identity(corruption, tmp_path):
    harness = StoreHarness("dynamo", tmp_path)
    store = harness.store()
    store.compare_and_set_bound(None, BOUND)
    item = harness.client.item
    if corruption == "identity": item["receiver_identity"]["M"]["native_generation"] = {"S": "changed"}
    elif corruption == "scope": item["scope_id"] = {"S": "changed"}
    elif corruption == "sequence": item["sequence"] = {"N": "30"}
    elif corruption == "digest": item["checkpoint_digest"] = {"S": "0" * 64}
    else: item["receiver_identity"] = {"M": []}
    before = harness.durable_bytes()
    for operation in (store.load, store.load_bound,
                      lambda: store.compare_and_set_bound(BOUND, replace(BOUND, sequence=30))):
        with pytest.raises(ValueError):
            operation()
        assert harness.durable_bytes() == before


@pytest.mark.parametrize("backend", ["json", "gcs"])
@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"format_version":"as400-checkpoint-v1",'
    b'"receiver":"R1","sequence":20,"sequence":21}'])
def test_json_checkpoint_parsing_never_drops_duplicate_fields_or_invalid_shapes(backend, payload, tmp_path):
    harness = StoreHarness(backend, tmp_path)
    if backend == "json":
        harness.path.write_bytes(payload)
    else:
        harness.client.objects[("sandbox-state", harness.store().object_name)] = (payload, {}, 1)
    for operation in (harness.store().load, harness.store().load_bound,
                      lambda: harness.store().compare_and_set_bound(None, BOUND)):
        with pytest.raises(ValueError):
            operation()
        assert harness.durable_bytes() == payload


def test_dynamo_bound_cas_supports_the_existing_custom_partition_key():
    client = AtomicDynamoClient()
    store = DynamoDbCheckpointStore("checkpoints", "stream-a", client=client, partition_key="stream_id")
    store.compare_and_set_bound(None, BOUND)
    assert store.load_bound() == BOUND
    assert set(client.get_calls[-1]["Key"]) == {"stream_id"}
    assert "stream_id" in client.item and "pk" not in client.item


@pytest.mark.parametrize("previous,candidate", [(BOUND.position, BOUND), (None, BOUND.to_dict())])
def test_bound_cas_rejects_wrong_envelope_types_without_persisting(harness, previous, candidate):
    with pytest.raises(ValueError):
        harness.store().compare_and_set_bound(previous, candidate)
    assert harness.store().load() is None
