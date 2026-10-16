"""Stored preparation closes actual originals without admitting native proof."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.copy_observation import build_context, encode, publish_copy_observations
from quadringent.object_store import FileObjectStore
from quadringent.raw import RawBatchWriter, bind_snapshot_batch
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.storage_backend import StorageBackend, registry_authority
from quadringent.storage_layout import snapshot_prefix
from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import (
    InitialCopyEvidence,
    SnapshotBatchRef,
    copy_intent_key,
    evidence_key,
)
from quadringent_control_plane.v2.services.native_context import context_snapshot
from quadringent_control_plane.v2.services.native_preparation import (
    NativePreparationError,
    PreparationLimits,
    StoredNativePreparer,
)
from quadringent_control_plane.v2.services.native_verifications import NativePreparation
from test_copy_observation import observation, request as observation_request
from test_gcs_backend import FakeGcsClient
from test_v2_native_verifications import authority  # noqa: F401


ROOT = "raw/test"
REVISION = "a" * 40


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class OfflineAws:
    def __init__(self):
        self.objects = {}
        self.item = None
        self.checkpoint_reads = self.checkpoint_writes = 0
        self.unbounded_reads = 0

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise MissingObject()
        return {"Metadata": {"sha256": hashlib.sha256(self.objects[Key]).hexdigest()}}

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise MissingObject()
        owner = self

        class Body(BytesIO):
            def read(self, size=-1):
                if size < 0:
                    owner.unbounded_reads += 1
                return super().read(size)

        return {"Body": Body(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch):
        assert IfNoneMatch == "*"
        assert Key not in self.objects
        self.objects[Key] = Body

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        self.checkpoint_reads += 1
        return {"Item": deepcopy(self.item)} if self.item is not None else {}

    def put_item(self, **kwargs):
        self.checkpoint_writes += 1
        self.item = deepcopy(kwargs["Item"])


def load_reference(case, artifact):
    key = urlsplit(artifact.uri).path.lstrip("/")
    raw = case.storage.object_store("").get_bounded(key, artifact.byte_count)
    assert len(raw) == artifact.byte_count
    assert hashlib.sha256(raw).hexdigest() == artifact.sha256
    return raw


@pytest.fixture
def case(authority, tmp_path, request):
    engine, _ = authority
    columns = [
        {"name": "ID", "kind": "integer", "nullable": False},
        {"name": "AMOUNT", "kind": "decimal", "precision": 38, "scale": 12, "nullable": False},
        {"name": "LONG_BUSINESS_LABEL", "kind": "char", "length": 8, "nullable": False},
    ]
    with engine.begin() as c:
        c.execute(schema.tables.update().where(schema.tables.c.id == "table-one").values(discovered_columns=columns))
        _, context, _, _ = context_snapshot(c, "org-one", "pipeline-one")
    data = tmp_path / "durable"
    data.mkdir()
    (data / ".quadringent-local").write_text(json.dumps({"format_version": 1, "label": "synthetic-proof"}))
    aws = OfflineAws() if getattr(request, "param", "local") == "aws" else None
    gcs = FakeGcsClient() if getattr(request, "param", "local") == "gcs" else None
    storage = (
        StorageBackend("aws", "synthetic-proof", "checkpoint-table", aws_client_factory=lambda _: aws)
        if aws is not None
        else StorageBackend("local", "synthetic-proof", str(data))
    )
    if gcs is not None:
        storage = StorageBackend("gcs", "synthetic-proof", "synthetic-checkpoints", gcs_client=gcs)
    store = storage.object_store("")
    stream = reader_checkpoint_stream_key(
        storage_backend=storage.kind,
        source_id="source-one",
        journal_library="TESTLIB",
        journal_name="JRN",
        raw_bucket=storage.raw_bucket,
        raw_prefix=ROOT,
    )
    registry = ReceiverNameRegistry(
        storage.object_store("receiver-authority-v1"),
        authority=registry_authority(storage.kind, storage.raw_bucket),
        source_system="ibmi",
        journal="JRN",
    )
    native = getattr(request, "param", "local") == "native"
    identity = ReceiverIdentity("TESTLIB", "JRN", "TESTLIB", "RCV1",
                                "ibmi-ifs-object-v1" if native else "offline-v1",
                                "1:123:456:789" if native else "generation-one")
    binding = registry.create_or_load_binding(scope_id=stream, identity=identity, sequence=20)
    checkpoint = binding.checkpoint(identity, 21)
    storage.checkpoint_store(stream).compare_and_set_bound(None, checkpoint)
    boundary = JournalBoundary(
        "TESTLIB", "RCV1", 20, datetime(2026, 10, 16, 7, 59, tzinfo=timezone.utc), identity
    ).with_binding(binding)
    intent_key = "receiver-authority-v1/" + copy_intent_key("pipeline-one", "table-one", "run-one")
    intent = {
        "format_version": "quadringent-copy-intent-v1",
        "pipeline_id": "pipeline-one",
        "table_id": "table-one",
        "run_id": "run-one",
        "boundary": boundary.to_dict(),
    }
    intent_bytes = (json.dumps(intent, indent=2) + "\n").encode()
    store.put_once(intent_key, intent_bytes)
    event = ChangeEvent(
        "ibmi",
        "SNAPSHOT:TESTLIB.ITEMS",
        "TESTLIB",
        "ITEMS",
        "c",
        JournalPosition("SNAPSHOT:run-one", 1),
        "2026-10-16T08:01:00Z",
        "snapshot-v1",
        None,
        {"ID": 1, "AMOUNT": "123.45" + "0" * 10, "LONG_BUSINESS_LABEL": "ABC     "},
    )
    staging = tmp_path / "snapshot"
    batch = RawBatchWriter(staging).write_batch((event,), high_watermark=event.position)
    payload = (staging / f"batch-{batch.batch_id}.jsonl").read_bytes()
    manifest = bind_snapshot_batch(
        (staging / f"batch-{batch.batch_id}.manifest.json").read_bytes(),
        payload,
        binding=binding,
        boundary=binding.bootstrap,
        pipeline_id="pipeline-one",
        table_id="table-one",
        run_id="run-one",
    )
    prefix = snapshot_prefix(ROOT, "ITEMS")
    bound_batch_id = json.loads(manifest)["batch_id"]
    payload_key, manifest_key = f"batch-{bound_batch_id}.jsonl", f"batch-{bound_batch_id}.manifest.json"
    store.put_once(prefix + "/" + payload_key, payload)
    store.put_once(prefix + "/" + manifest_key, manifest)
    key = evidence_key(ROOT, "table-one", "run-one")
    req = observation_request()
    req["witness_window"].update(receiver_name="RCV1", end="20")
    current = build_context(
        pipeline_id="pipeline-one", table_id="table-one", run_id="run-one", boundary=boundary, request=req
    )
    inventory = [
        {
            "prefix": prefix,
            "payload_key": payload_key,
            "manifest_key": manifest_key,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "payload_bytes": len(payload),
            "manifest_bytes": len(manifest),
        }
    ]
    observations = publish_copy_observations(
        storage.object_store("receiver-authority-v1"),
        context=current,
        before_bytes=encode(observation(current=current)),
        after_bytes=encode(observation("after", current)),
        snapshot_inventory=inventory,
        rows_copied=1,
        evidence_key=key,
    )
    evidence = InitialCopyEvidence(
        "pipeline-one",
        "table-one",
        "run-one",
        boundary,
        1,
        datetime(2026, 10, 16, 8, 3, tzinfo=timezone.utc),
        (SnapshotBatchRef(payload_key, manifest_key),),
        observations,
    )
    store.put_once(key, encode(evidence.to_dict()))
    return SimpleNamespace(
        storage=storage,
        store=store,
        context=context,
        stream=stream,
        binding=binding,
        checkpoint=checkpoint,
        registry=registry,
        evidence=evidence,
        evidence_key=key,
        intent_key=intent_key,
        intent_bytes=intent_bytes,
        data=data,
        payload_key=prefix + "/" + payload_key,
        manifest_key=prefix + "/" + manifest_key,
        aws=aws,
        gcs=gcs,
    )


def preparer(case, **kwargs):
    return StoredNativePreparer(case.storage, ROOT, "org-one", REVISION, "0.4.5", **kwargs)


def manifest(case, result):
    return json.loads(load_reference(case, next(a for a in result.artifacts if a.name == "preparation_manifest")))


def test_real_stored_preparation_keeps_original_bytes_and_false_native_scope(case):
    result = preparer(case).prepare(case.context)
    assert type(result) is NativePreparation
    assert result.scope.checkpoint == case.checkpoint
    assert result.scope.copy_boundary == case.binding.bootstrap
    assert result.scope.history_table == "ITEMS_HISTORY"
    assert result.scope.mirror_table == "ITEMS_MIRROR"
    assert result.scope.expected_snapshot_rows == 1
    assert [c.name for c in result.scope.columns] == ["ID", "AMOUNT", "LONG_BUSINESS_LABEL"]
    artifacts = {a.name: a for a in result.artifacts}
    assert set(artifacts) == {
        "copy_intent",
        "copy_evidence",
        "registry",
        "binding",
        "checkpoint",
        "preparation_manifest",
    }
    assert load_reference(case, artifacts["copy_intent"]) == case.intent_bytes
    assert load_reference(case, artifacts["checkpoint"]) == case.storage.checkpoint_store(case.stream).path.read_bytes()
    document = manifest(case, result)
    assert document["native_admission"] == "not_evaluated"
    assert document["native_cdc_qualified"] is False
    assert document["registry_inventory"]["coverage"] == "exact_keys_used"
    assert document["checkpoint_observation"]["encoding"] == "checkpoint-json-v2"
    assert document["checkpoint_observation"]["stream_key"] == case.stream
    keys = {item["source_key"] for item in document["originals"]}
    assert {case.intent_key, case.evidence_key, case.payload_key, case.manifest_key} <= keys
    for item in document["originals"]:
        raw = case.store.get_bounded(item["source_key"], 32 * 1024**2)
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert "native-preparations-v1/" in item["archived_uri"]


def test_constructor_has_no_backend_calls_and_requires_installed_revision(case, monkeypatch):
    monkeypatch.setattr(StorageBackend, "object_store", lambda *_: pytest.fail("constructor performed I/O"))
    preparer(case)
    with pytest.raises(NativePreparationError):
        StoredNativePreparer(case.storage, ROOT, "org-one", "unknown", "0.4.5")


@pytest.mark.parametrize("damage", ["org", "source-config", "source", "journal", "run", "pk", "columns", "destination"])
def test_context_scope_mismatch_never_prepares(case, damage):
    context = deepcopy(case.context)
    if damage == "org":
        context["org_id"] = "org-other"
    elif damage == "source-config":
        context["storage"] = {"raw_bucket": "foreign"}
    elif damage == "source":
        context["source_id"] = "source-other"
    elif damage == "journal":
        context["table"]["journal_name"] = "OTHER"
    elif damage == "run":
        context["copy_run_id"] = "run-other"
    elif damage == "pk":
        context["table"]["key_columns"] = "MISSING"
    elif damage == "columns":
        context["table"]["discovered_columns"].reverse()
    else:
        context["destination"]["destination_database"] = "../unsafe"
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(context)


@pytest.mark.parametrize(
    "damage", ["legacy", "missing-after", "missing-raw", "changed-raw", "intent-f", "duplicate-json", "nan-json"]
)
def test_dependency_closure_and_strict_json_are_required(case, damage):
    root = case.store.root
    if damage == "legacy":
        value = replace(case.evidence, copy_observations=None).to_dict()
        (root / case.evidence_key).write_bytes(encode(value))
    elif damage == "missing-after":
        doc = json.loads((root / "receiver-authority-v1" / case.evidence.copy_observations.key).read_bytes())
        (root / "receiver-authority-v1" / doc["after"]["key"]).unlink()
    elif damage == "missing-raw":
        (root / case.payload_key).unlink()
    elif damage == "changed-raw":
        with (root / case.manifest_key).open("ab") as output:
            output.write(b" ")
    elif damage == "intent-f":
        value = json.loads(case.intent_bytes)
        value["boundary"]["bound_checkpoint"]["sequence"] = 19
        (root / case.intent_key).write_bytes(encode(value))
    elif damage == "duplicate-json":
        raw = (root / case.evidence_key).read_bytes()
        (root / case.evidence_key).write_bytes(b'{"rows_copied":999,' + raw[1:])
    else:
        raw = (root / case.evidence_key).read_bytes()
        (root / case.evidence_key).write_bytes(raw.replace(b'"rows_copied":1', b'"rows_copied":NaN'))
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


@pytest.mark.parametrize("same", [True, False])
def test_checkpoint_observed_once_and_never_replaced_by_later_progress(case, monkeypatch, same):
    checkpoint_store = case.storage.checkpoint_store(case.stream)
    if same:
        checkpoint_store.path.write_bytes(encode(case.binding.bootstrap.to_dict()))
    original = checkpoint_store.observe_bound
    calls = []

    def observe():
        calls.append(True)
        value = original()
        checkpoint_store.path.write_bytes(
            encode(case.binding.checkpoint(case.checkpoint.receiver_identity, 30).to_dict())
        )
        return value

    monkeypatch.setattr(type(checkpoint_store), "observe_bound", lambda _: observe())
    result = preparer(case).prepare(case.context)
    assert len(calls) == 1
    expected = case.binding.bootstrap if same else case.checkpoint
    assert result.scope.checkpoint == expected
    assert result.scope.receiver_windows[0].end_sequence == expected.sequence
    assert result.scope.receiver_windows[0].start_sequence == 21


def test_rotation_without_original_chain_refuses_even_with_claim(case):
    other = ReceiverIdentity("TESTLIB", "JRN", "TESTLIB", "RCV2", "offline-v1", "generation-two")
    end = case.binding.checkpoint(other, 30)
    case.registry.claim(case.binding, end)
    case.storage.checkpoint_store(case.stream).path.write_bytes(encode(end.to_dict()))
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


@pytest.mark.parametrize("limit,value", [("max_objects", 2), ("max_total_bytes", 100), ("max_object_bytes", 10)])
def test_aggregate_budgets_fail_closed(case, limit, value):
    with pytest.raises(NativePreparationError):
        preparer(case, limits=PreparationLimits(**{limit: value})).prepare(case.context)


def test_provider_failure_is_sanitized(case, monkeypatch):
    def failure(*_):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_CANARY")

    monkeypatch.setattr(FileObjectStore, "get_bounded", failure)
    with pytest.raises(NativePreparationError) as error:
        preparer(case).prepare(case.context)
    assert "SYNTHETIC_PRIVATE_PROVIDER_CANARY" not in str(error.value)
    assert error.value.__cause__ is None


def test_changed_original_on_final_readback_refuses(case, monkeypatch):
    read = FileObjectStore.get_bounded
    count = 0

    def race(store, key, maximum):
        nonlocal count
        value = read(store, key, maximum)
        if store.root == case.store.root and key == case.evidence_key:
            count += 1
            if count > 1:
                return value + b" "
        return value

    monkeypatch.setattr(FileObjectStore, "get_bounded", race)
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


def test_failed_archive_put_once_is_not_treated_as_existing_success(case, monkeypatch):
    put = FileObjectStore.put_once

    def fail(store, key, content):
        if key.startswith("native-preparations-v1/"):
            raise OSError("SYNTHETIC_ARCHIVE_FAILURE")
        return put(store, key, content)

    monkeypatch.setattr(FileObjectStore, "put_once", fail)
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


@pytest.mark.parametrize("case", ["aws"], indirect=True)
def test_typed_dynamo_original_is_archived_to_s3_without_reconstructing_original(case):
    before_reads, before_writes = case.aws.checkpoint_reads, case.aws.checkpoint_writes
    original_item = json.dumps(case.aws.item, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    result = preparer(case).prepare(case.context)
    assert case.aws.checkpoint_reads == before_reads + 1
    assert case.aws.checkpoint_writes == before_writes
    artifact = next(a for a in result.artifacts if a.name == "checkpoint")
    assert artifact.uri.startswith("s3://synthetic-proof/native-preparations-v1/")
    assert load_reference(case, artifact) == original_item
    document = manifest(case, result)
    assert document["checkpoint_observation"]["encoding"] == "dynamodb-attribute-item-v1"
    assert document["checkpoint_observation"]["state_location"] == "checkpoint-table"
    assert json.loads(original_item)["sequence"] == {"N": "21"}


@pytest.mark.parametrize("damage", ["missing", "legacy"])
def test_checkpoint_must_be_present_and_native_bound(case, damage):
    path = case.storage.checkpoint_store(case.stream).path
    if damage == "missing":
        path.unlink()
    else:
        path.write_text('{"format_version":"as400-checkpoint-v1","receiver":"RCV1","sequence":21}')
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


def test_metadata_count_cannot_replace_actual_complete_snapshot_rows(case):
    from quadringent.copy_observation import ObservationReference

    key = "receiver-authority-v1/" + case.evidence.copy_observations.key
    doc = json.loads((case.store.root / key).read_bytes())
    doc["rows_copied"] = 2
    raw = encode(doc)
    (case.store.root / key).write_bytes(raw)
    reference = ObservationReference(case.evidence.copy_observations.key, hashlib.sha256(raw).hexdigest(), len(raw))
    changed = replace(case.evidence, rows_copied=2, copy_observations=reference)
    (case.store.root / case.evidence_key).write_bytes(encode(changed.to_dict()))
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


def test_deadline_crossed_during_storage_read_never_returns_success(case, monkeypatch):
    from quadringent_control_plane.v2.services import native_preparation as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    read = FileObjectStore.get_bounded

    def delayed(store, key, maximum):
        result = read(store, key, maximum)
        clock[0] = 1000.0
        return result

    monkeypatch.setattr(FileObjectStore, "get_bounded", delayed)
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


def test_durable_archive_readback_must_match_the_actual_original(case, monkeypatch):
    read = FileObjectStore.get_bounded

    def corrupt(store, key, maximum):
        result = read(store, key, maximum)
        return result + b" " if key.startswith("native-preparations-v1/") else result

    monkeypatch.setattr(FileObjectStore, "get_bounded", corrupt)
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)


@pytest.mark.parametrize("case", ["aws"], indirect=True)
def test_existing_wrong_archive_refuses_before_unbounded_put_once_fallback(case):
    raw = case.aws.objects[case.evidence_key]
    key = (
        "native-preparations-v1/"
        + hashlib.sha256(b"org-one").hexdigest()
        + "/objects/"
        + hashlib.sha256(raw).hexdigest()
    )
    case.aws.objects[key] = b"wrong content"
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(case.context)
    assert case.aws.unbounded_reads == 0


@pytest.mark.parametrize(
    "limit,value",
    [
        ("max_rows", True),
        ("max_seconds", 1.5),
        ("max_objects", 0),
        ("max_rows", 20_000_001),
        ("max_total_bytes", 16 * 1024**3 + 1),
        ("max_object_bytes", 64 * 1024**2 + 1),
    ],
)
def test_limit_types_and_hard_caps_are_explicit(limit, value):
    with pytest.raises(NativePreparationError):
        PreparationLimits(**{limit: value})


def test_foreign_org_and_nonfinite_context_never_open_storage(case, monkeypatch):
    monkeypatch.setattr(StorageBackend, "object_store", lambda *_: pytest.fail("invalid context opened storage"))
    foreign = deepcopy(case.context)
    foreign["org_id"] = "org-other"
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(foreign)
    invalid = deepcopy(case.context)
    invalid["source"]["ibmi_host"] = float("nan")
    with pytest.raises(NativePreparationError):
        preparer(case).prepare(invalid)


def test_returned_scope_uses_injected_build_identity_and_preparation_is_repeatable(case):
    first = preparer(case).prepare(case.context)
    second = preparer(case).prepare(case.context)
    assert first == second
    assert first.scope.producer_revision == REVISION
    assert first.scope.producer_version == "0.4.5"


@pytest.mark.parametrize("case", ["gcs"], indirect=True)
def test_gcs_generation_is_origin_metadata_not_archived_content_version(case):
    records = [(key, value) for key, value in case.gcs.objects.items() if key[0] == "synthetic-checkpoints"]
    assert len(records) == 1
    _, (raw, _, generation) = records[0]
    result = preparer(case).prepare(case.context)
    artifact = next(a for a in result.artifacts if a.name == "checkpoint")
    assert load_reference(case, artifact) == raw
    assert artifact.uri.startswith("gcs://synthetic-proof/native-preparations-v1/")
    assert artifact.version == "sha256:" + artifact.sha256
    document = manifest(case, result)
    assert document["checkpoint_observation"]["version"] == "gcs-generation:" + str(generation)
    assert document["checkpoint_observation"]["encoding"] == "checkpoint-json-v2"
    assert json.loads(raw)["stream_id"] == case.stream
