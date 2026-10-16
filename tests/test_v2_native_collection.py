"""Server-configured producers execute offline; collection is not admission."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from quadringent.qualification.verification_native import load_native_copy_inputs
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.object_store import RawFirstCaptureCoordinator
from quadringent.raw import RawBatchWriter, bind_raw_batch
from quadringent.qualification.verification_stored import (
    BatchReference,
    ObjectReference,
    StoredReadSet,
    export_stored,
)
from quadringent.qualification.verification_stream import FrameReader, StreamLimits, write_stream
from quadringent.storage_layout import journal_prefix
from quadringent_control_plane.v2.services import native_collection as service
from test_native_verification_destination import fixture as destination_fixture
from test_native_verification_stored import record
from test_v2_native_preparation import ROOT, case, preparer  # noqa: F401
from test_v2_native_verifications import authority  # noqa: F401


STAMP = "2026-10-16T08:10:00Z"
SECRET = "SYNTHETIC_STDIN_PASSWORD"
BUSINESS = "io.quadringent.as400."


class Authority:
    def __init__(self, store):
        self.store = store

    def get_bounded(self, key, maximum):
        return self.store.get_bounded("receiver-authority-v1/" + key, maximum)


def rows_from(output, scope, dataset):
    output.seek(0)
    return list(FrameReader(output, scope_sha256=scope.sha256, dataset=dataset))


def object_ref(store, key):
    raw = store.get_bounded(key, 32 * 1024**2)
    return ObjectReference(key, hashlib.sha256(raw).hexdigest())


def artifact_bytes(case, artifact):
    key = urlsplit(artifact.uri).path.lstrip("/")
    raw = case.storage.object_store("").get_bounded(key, artifact.byte_count)
    assert len(raw) == artifact.byte_count
    assert hashlib.sha256(raw).hexdigest() == artifact.sha256
    return raw


def make_collection(case, tmp_path, *, broken_chain=False):
    if broken_chain:
        point = case.binding.checkpoint(case.checkpoint.receiver_identity, 22)
        checkpoint_store = case.storage.checkpoint_store(case.stream)
        checkpoint_store.compare_and_set_bound(case.checkpoint, point)
        assert checkpoint_store.load_bound() == point
    prepared = preparer(case).prepare(case.context)
    scope = prepared.scope
    initial = object_ref(case.store, case.evidence_key)
    intent = object_ref(case.store, case.intent_key)
    original = {"ID": 1, "AMOUNT": "123.45" + "0" * 10, "LONG_BUSINESS_LABEL": "ABC     "}
    second = {"ID": 2, "AMOUNT": None, "LONG_BUSINESS_LABEL": "SECOND  "}
    if broken_chain:
        before = {**original, "LONG_BUSINESS_LABEL": "BEFORE  "}
        events = [record(scope, 21, before, operation="u_before"), record(scope, 22, original, operation="u_after")]
        final = [original]
    else:
        events, final = [record(scope, 21, second)], [original, second]
    staging = tmp_path / "journal-staging"
    batch = RawBatchWriter(staging).write_batch(events, high_watermark=events[-1].position)
    payload = (staging / f"batch-{batch.batch_id}.jsonl").read_bytes()
    raw_manifest = bind_raw_batch(
        (staging / f"batch-{batch.batch_id}.manifest.json").read_bytes(),
        payload,
        binding=scope.capture_binding,
        end=scope.checkpoint,
    )
    batch_id = json.loads(raw_manifest)["batch_id"]
    prefix = journal_prefix(ROOT, scope.source_table)
    publisher_checkpoint = JsonCheckpointStore(tmp_path / "publisher-checkpoint.json")
    publisher_checkpoint.compare_and_set_bound(None, scope.copy_boundary)
    assert publisher_checkpoint.load_bound() == scope.copy_boundary
    journal_store = case.storage.object_store(prefix)
    receipt = (
        RawFirstCaptureCoordinator(journal_store, publisher_checkpoint)
        .capture_receipted_window_result(
            start=scope.capture_binding.checkpoint(scope.checkpoint.receiver_identity, 21),
            end=scope.checkpoint,
            previous=scope.copy_boundary,
            capture_binding=scope.capture_binding,
            manifest_content=raw_manifest,
            payload=payload,
            scan_completed_at=datetime.fromisoformat(STAMP),
        )
        .receipt
    )
    identity = json.dumps(receipt["start"], sort_keys=True, separators=(",", ":")).encode()
    receipt_key = (
        journal_prefix(ROOT, scope.source_table) + "/receipts/scan-" + hashlib.sha256(identity).hexdigest() + ".json"
    )
    journal = BatchReference(
        object_ref(case.store, prefix + f"/batch-{batch_id}.manifest.json"),
        object_ref(case.store, prefix + f"/batch-{batch_id}.jsonl"),
        object_ref(case.store, receipt_key),
    )
    snapshot = BatchReference(object_ref(case.store, case.manifest_key), object_ref(case.store, case.payload_key))
    read_set = StoredReadSet(scope.sha256, (snapshot, journal))
    layers = {"source": final, "mirror": deepcopy(final)}
    for dataset in ("snapshot", "raw"):
        output = BytesIO()
        export_stored(
            case.store,
            scope,
            output,
            dataset=dataset,
            raw_prefix_root=ROOT,
            initial_copy=initial,
            copy_intent=intent,
            raw_read_set=read_set if dataset == "raw" else None,
        )
        layers[dataset] = rows_from(output, scope, dataset)
    layers["journal"] = layers["raw"][len(layers["snapshot"]) :]
    layers["history"] = layers["raw"]
    layers["positions"] = [
        {name: row[name] for name in ("JOURNAL_RECEIVER", "JOURNAL_SEQUENCE")} for row in layers["journal"]
    ]
    evidence = case.store.get_bounded(initial.key, 65536)
    native = load_native_copy_inputs(
        Authority(case.store), evidence, expected_sha256=initial.sha256, evidence_key=initial.key, scope=scope
    )
    from test_native_source_observation import guard_documents
    journal_guard, final_guard = guard_documents(native, scope)
    data = {
        "guard_journal": journal_guard,
        "guard_final": final_guard,
        "metadata": {
            "scope_sha256": scope.sha256,
            "context_sha256": native.context_sha256,
            "capture_binding_sha256": native.binding_sha256,
            "copy_boundary_sha256": native.boundary_sha256,
            "copy_run_id": scope.copy_run_id,
            "bundle_sha256": native.bundle_sha256,
        }
    }
    for dataset in ("source", "journal", "positions"):
        output = BytesIO()
        write_stream(
            output, layers[dataset], scope_sha256=scope.sha256, dataset=dataset, started_at=STAMP, completed_at=STAMP
        )
        data[dataset] = output.getvalue().decode()
    fixture = tmp_path / "producer-data.json"
    fixture.write_text(json.dumps(data))
    calls = tmp_path / "producer-calls.jsonl"
    executable = tmp_path / "java-fixture"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ["QDT_FIXTURE"]).read_text())
mode = os.environ.get("QDT_MODE", "")
assert sys.stdin.buffer.read().endswith(b"SYNTHETIC_STDIN_PASSWORD\n")
kind = sys.argv[3].rsplit(".", 1)[-1]
with open(os.environ["QDT_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\n")
def guard(mode):
    return data["guard_journal" if mode == "journal" else "guard_final"]
def save(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
if kind == "NativeVerificationSourceExport":
    payload = data["source"]
    if mode == "source-exit":
        sys.stderr.write("SYNTHETIC_STDIN_PASSWORD")
        sys.exit(7)
    if mode == "source-truncated":
        payload = payload.rsplit("\n", 2)[0] + "\n"
elif kind == "NativeVerificationPositionsExport":
    payload = data["positions"]
elif kind == "NativeBusinessImageExport":
    doc = guard("journal")
    if mode == "journal-guard":
        doc["copy_run_id"] = "wrong-run"
    save(sys.argv[27], (json.dumps(doc) + "\n").encode())
    payload = data["journal"]
elif kind == "NativeLifecycleGuard":
    doc = guard("after_all_streams")
    if mode == "final-guard":
        doc["guards"][0]["phase"] = "before_business"
    payload = json.dumps(doc) + "\n"
    save(sys.argv[27], payload.encode())
    if mode == "final-receipt":
        payload = payload.replace('"complete"', '"wrong"')
else:
    sys.exit(9)
sys.stdout.buffer.write(payload.encode())
"""
    )
    executable.chmod(0o700)
    connections = []

    def connection_factory():
        dataset = "history" if not connections else "mirror"
        names = [column.name for column in scope.columns]
        if dataset == "history":
            names += ["EVENT_ID", "OPERATION", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE"]
        rows = [
            tuple(
                Decimal(str(row[name]))
                if row[name] is not None and name in {"ID", "AMOUNT", "JOURNAL_SEQUENCE"}
                else row[name]
                for name in names
            )
            for row in layers[dataset]
        ]
        _, connection = destination_fixture(dataset=dataset, custom_scope=scope, rows=rows)
        connection.closed = False
        connection.close = lambda: setattr(connection, "closed", True)
        connections.append(connection)
        return connection

    credentials = (
        case.context["source"]["ibmi_host"] + "\n" + case.context["source"]["ibmi_user"] + "\n" + SECRET + "\n"
    ).encode()
    config = dict(
        java=str(executable),
        classpath=str(tmp_path / "reviewed-classpath"),
        credentials=credentials,
        environment={"QDT_FIXTURE": str(fixture), "QDT_CALLS": str(calls)},
        source_id="source-one",
        destination_id="destination-one",
        connection_factory=connection_factory,
    )
    return SimpleNamespace(
        case=case,
        prepared=prepared,
        config=config,
        layers=layers,
        native=native,
        connections=connections,
        calls=calls,
        directory=tmp_path / "collection",
        raw_batch=journal,
    )


@pytest.fixture
def collected_case(case, tmp_path):
    return make_collection(case, tmp_path)


def collector(value, **changes):
    return service.StoredNativeCollector(value.case.storage, ROOT, "org-one", **{**value.config, **changes})


def run(value, **changes):
    return collector(value).collect(
        value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True, **changes
    )


def test_server_builds_real_seven_producers_and_archives_them(collected_case):
    value = collected_case
    result = run(value)
    assert result.report["state"] == "verified"
    assert result.native_admission == "not_evaluated" and result.native_cdc_qualified is False
    assert result.report["transaction_consistency_verified"] is False
    assert all(connection.closed for connection in value.connections) and len(value.connections) == 2
    manifest = json.loads(artifact_bytes(value.case, result.manifest))
    assert manifest["scope_sha256"] == value.prepared.scope.sha256
    assert manifest["native_admission"] == "not_evaluated" and manifest["native_cdc_qualified"] is False
    assert set(manifest["streams"]) == {"source", "snapshot", "journal", "positions", "raw", "history", "mirror"}
    for artifact in result.artifacts:
        raw = artifact_bytes(value.case, artifact)
        assert SECRET.encode() not in raw
        assert b"QDT_FIXTURE" not in raw
    calls = [json.loads(line) for line in value.calls.read_text().splitlines()]
    assert [call[2].removeprefix(BUSINESS) for call in calls] == [
        "NativeVerificationSourceExport",
        "NativeBusinessImageExport",
        "NativeVerificationPositionsExport",
        "NativeLifecycleGuard",
    ]
    assert calls[0][3:10] == [
        "TESTLIB",
        "ITEMS",
        "TESTLIB",
        "JRN",
        "ID",
        "ID,AMOUNT,LONG_BUSINESS_LABEL",
        value.prepared.scope.sha256,
    ]
    assert calls[2][3:12] == [
        "TESTLIB",
        "JRN",
        "TESTLIB",
        "RCV1",
        "TESTLIB",
        "ITEMS",
        "21",
        "21",
        value.prepared.scope.sha256,
    ]
    assert SECRET not in repr(result) and SECRET not in repr(collector(value))
    for directory, _, names in os.walk(value.directory):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert all(stat.S_IMODE(os.stat(Path(directory) / name).st_mode) == 0o600 for name in names)


def test_divergent_destination_is_preserved_as_collection(collected_case):
    value = collected_case
    value.layers["mirror"][0]["AMOUNT"] = "999.00"
    result = run(value)
    assert result.report["state"] == "divergent"
    assert result.report["comparisons"]["mirror"]["equal"] is False
    assert result.native_cdc_qualified is False
    assert artifact_bytes(value.case, result.manifest)


def test_late_snapshot_does_not_admit_broken_row_image_chain(case, tmp_path):
    value = make_collection(case, tmp_path, broken_chain=True)
    result = run(value)
    assert result.report["state"] == "divergent"
    assert all(check["equal"] for check in result.report["comparisons"].values())
    assert result.report["checks"]["journal_row_images_reconstructed"] is False


@pytest.mark.parametrize("mode", ["source-exit", "source-truncated", "journal-guard", "final-guard", "final-receipt"])
def test_incomplete_producer_or_guard_never_returns_success(collected_case, mode):
    value = collected_case
    env = {**value.config["environment"], "QDT_MODE": mode}
    with pytest.raises(service.NativeCollectionError, match="^native_collection_failed$") as error:
        collector(value, environment=env).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True
        )
    assert SECRET not in str(error.value) and error.value.__cause__ is None
    assert not value.directory.exists()


@pytest.mark.parametrize("field", ["source_id", "destination_id", "org_id"])
def test_foreign_config_is_refused_before_process(collected_case, field):
    value = collected_case
    context = deepcopy(value.case.context)
    context[field] = "foreign"
    with pytest.raises(service.NativeCollectionError):
        collector(value).collect(value.prepared, context=context, directory=value.directory, check_current=lambda: True)
    assert not value.calls.exists()


@pytest.mark.parametrize("current", [False, 1, None])
def test_initial_fence_requires_boolean_true(collected_case, current):
    value = collected_case
    with pytest.raises(service.NativeCollectionError):
        collector(value).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: current
        )
    assert not value.calls.exists() and not value.directory.exists()


def test_changed_configuration_after_source_refuses_return(collected_case):
    value = collected_case

    def current():
        return not value.calls.exists()

    with pytest.raises(service.NativeCollectionError):
        collector(value).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=current
        )
    assert len(value.calls.read_text().splitlines()) == 1
    assert not value.directory.exists()


def test_archived_preparation_corruption_refuses_before_source(collected_case):
    value = collected_case
    artifact = next(a for a in value.prepared.artifacts if a.name == "copy_evidence")
    key = urlsplit(artifact.uri).path.lstrip("/")
    value.case.store._path_for(key).write_bytes(b"changed original")
    with pytest.raises(service.NativeCollectionError):
        run(value)
    assert not value.calls.exists()


def test_missing_raw_payload_is_incomplete_before_source(collected_case):
    value = collected_case
    value.case.store._path_for(value.raw_batch.payload.key).unlink()
    with pytest.raises(service.NativeCollectionError):
        run(value)
    assert not value.calls.exists()


def test_existing_directory_is_not_removed_or_reused(collected_case):
    value = collected_case
    value.directory.mkdir()
    (value.directory / "keep").write_text("unrelated")
    with pytest.raises(service.NativeCollectionError):
        run(value)
    assert (value.directory / "keep").read_text() == "unrelated"


def test_unmatched_preparation_is_rejected(collected_case):
    value = collected_case
    changed = replace(value.prepared, scope=replace(value.prepared.scope, copy_run_id="other-run"))
    with pytest.raises(service.NativeCollectionError):
        collector(value).collect(
            changed, context=value.case.context, directory=value.directory, check_current=lambda: True
        )
    assert not value.calls.exists()


def test_new_connection_is_closed_if_configuration_changes(collected_case):
    value = collected_case
    current = True

    def factory():
        nonlocal current
        connection = value.config["connection_factory"]()
        current = False
        return connection

    with pytest.raises(service.NativeCollectionError):
        collector(value, connection_factory=factory).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: current
        )
    assert len(value.connections) == 1 and value.connections[0].closed
    assert not value.directory.exists()


def test_original_mutation_before_durable_transfer_refuses(collected_case, monkeypatch):
    value = collected_case
    original = service.collect_verification

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.streams["source"].path.write_bytes(b"changed")
        return result

    monkeypatch.setattr(service, "collect_verification", changed)
    with pytest.raises(service.NativeCollectionError):
        run(value)
    assert not value.directory.exists()


def test_stream_transfer_failure_does_not_publish_collection(collected_case, monkeypatch):
    value = collected_case
    original = service.ArtifactArchive.retain_file
    attempts = []

    def failed(archive, name, *args, **kwargs):
        attempts.append(name)
        if name == "stream:source":
            raise OSError(SECRET)
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(service.ArtifactArchive, "retain_file", failed)
    with pytest.raises(service.NativeCollectionError) as error:
        run(value)
    assert SECRET not in str(error.value) and error.value.__cause__ is None
    assert "stream:source" in attempts and not value.directory.exists()


def test_supersession_during_final_archive_refuses_result(collected_case, monkeypatch):
    value = collected_case
    original = service.ArtifactArchive.retain_bytes
    current = True

    def changed(archive, name, content):
        nonlocal current
        result = original(archive, name, content)
        if name == "collection_manifest":
            current = False
        return result

    monkeypatch.setattr(service.ArtifactArchive, "retain_bytes", changed)
    with pytest.raises(service.NativeCollectionError):
        collector(value).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: current
        )
    assert not value.directory.exists()


def test_constructor_does_not_create_storage_clients(collected_case, monkeypatch):
    value = collected_case

    def unexpected(*args, **kwargs):
        pytest.fail("constructor opened storage")

    monkeypatch.setattr(type(value.case.storage), "object_store", unexpected)
    assert collector(value)


def test_credentials_must_match_frozen_source_context(collected_case):
    value = collected_case
    with pytest.raises(service.NativeCollectionError):
        collector(value, credentials=b"other.invalid\nOTHER\nSYNTHETIC_STDIN_PASSWORD\n").collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True
        )
    assert not value.calls.exists()


@pytest.mark.parametrize(
    "streams",
    [StreamLimits(max_rows=1), StreamLimits(max_bytes=100), StreamLimits(max_line_bytes=20)],
)
def test_real_process_frames_obey_server_limits(collected_case, streams):
    value = collected_case
    limits = service.CollectionLimits(streams=streams)
    with pytest.raises(service.NativeCollectionError):
        collector(value, limits=limits).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True
        )
    assert not value.directory.exists()


def test_global_deadline_after_process_refuses_success(collected_case, monkeypatch):
    value = collected_case
    now = 0.0

    def current():
        nonlocal now
        if value.calls.exists():
            now = 6.0
        return True

    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(service.NativeCollectionError):
        collector(value, limits=service.CollectionLimits(max_seconds=5)).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=current
        )
    assert len(value.calls.read_text().splitlines()) == 1
    assert not value.directory.exists()


@pytest.mark.parametrize("limit", [0, True, 1.5, 7201])
def test_invalid_overall_budget_is_rejected(limit):
    with pytest.raises(ValueError):
        service.CollectionLimits(max_seconds=limit)
