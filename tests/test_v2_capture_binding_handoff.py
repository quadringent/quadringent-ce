"""The installed copy handoff preserves the original native binding offline."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent_control_plane.v2.executor.boundary import BoundaryError, JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, copy_intent_key
from quadringent_control_plane.v2.executor.manifests import build_initial_copy_job, build_reader_deployment
from test_v2_executor_manifest_script_contracts import _container_env, _copy_spec, _reader_spec
from capture_binding_fakes import MemoryCaptureStorage


IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")
SCOPE = reader_checkpoint_stream_key(storage_backend="gcs", source_id="src-1", journal_library="TESTLIB",
                                     journal_name="TESTJRN", raw_bucket="quadringent-raw-example", raw_prefix="raw/example")
POINT = BoundCheckpoint(SCOPE, "capture-epoch", IDENTITY, 4200)
BINDING = CaptureBinding(SCOPE, "capture-epoch", "ibmi", "TESTJRN",
                         "gcs://quadringent-raw-example/receiver-authority-v1", POINT)
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def boundary(sequence=4200):
    return JournalBoundary("TESTLIB", "RCV0001", sequence, NOW,
                           receiver_identity=IDENTITY).with_binding(BINDING)


def test_bound_boundary_round_trip_preserves_the_original_bootstrap():
    current = boundary(4210)
    restored = JournalBoundary.from_dict(current.to_dict())
    assert restored == current
    assert restored.require_bound().sequence == 4210
    assert restored.capture_binding.bootstrap == POINT


@pytest.mark.parametrize("change", ["missing-binding", "unknown-version", "foreign-identity", "extra-field"])
def test_versioned_boundary_rejects_incomplete_or_foreign_contract(change):
    record = boundary().to_dict()
    if change == "missing-binding":
        record.pop("capture_binding")
    elif change == "unknown-version":
        record["format_version"] = "unsupported"
    elif change == "foreign-identity":
        record["bound_checkpoint"]["receiver_identity"]["native_generation"] = "foreign"
    else:
        record["unexpected"] = True
    with pytest.raises((BoundaryError, ValueError)):
        JournalBoundary.from_dict(record)


def test_historical_boundary_is_readable_but_never_adopted():
    old = JournalBoundary.from_dict({"receiver_library": "TESTLIB", "receiver_name": "RCV0001",
                                    "last_sequence": 4200, "observed_at": NOW.isoformat()})
    with pytest.raises(BoundaryError, match="bound"):
        old.require_bound()
    with pytest.raises(BoundaryError):
        old.with_binding(BINDING)


def test_copy_manifest_resolves_real_config_and_writes_complete_evidence():
    import quadringent_initial_copy_job as copy_job

    manifest = build_initial_copy_job(_copy_spec(boundary=boundary()))
    env = _container_env(manifest)
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    config = copy_job.InitialCopyConfig.from_environment(env)
    evidence = copy_job.build_evidence(config, rows_copied=2, completed_at=NOW)
    restored = InitialCopyEvidence.from_dict(evidence)
    assert restored.require_bound() == boundary().require_bound()
    assert restored.boundary.capture_binding == BINDING
    assert json.loads(env["AS400_CAPTURE_BINDING_JSON"]) == BINDING.to_dict()
    assert json.loads(env["AS400_BOUNDARY_JSON"]) == boundary().to_dict()


@pytest.mark.parametrize("missing", ["AS400_CAPTURE_BINDING_JSON", "AS400_BOUNDARY_JSON"])
def test_installed_copy_config_rejects_missing_contract_before_snapshot(missing):
    import quadringent_initial_copy_job as copy_job

    env = _container_env(build_initial_copy_job(_copy_spec(boundary=boundary())))
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    env.pop(missing)
    with pytest.raises(copy_job.InitialCopyJobError):
        copy_job.InitialCopyConfig.from_environment(env)


@pytest.mark.parametrize("name", ["AS400_CAPTURE_BINDING_JSON", "AS400_BOUNDARY_JSON"])
def test_reader_extra_environment_cannot_override_binding(name):
    from quadringent_control_plane.v2.executor.manifests import ManifestError, TableBootstrap

    table = TableBootstrap("tbl-orders", "TESTLIB", "QDC_ORDERS", boundary())
    with pytest.raises(ManifestError):
        build_reader_deployment(_reader_spec(tables=(table,), extra_env={name: "{}"}))


def test_shared_reader_keeps_original_binding_with_later_table_frontiers():
    from quadringent_control_plane.v2.executor.manifests import TableBootstrap

    tables = (TableBootstrap("a", "TESTLIB", "QDC_ORDERS", boundary()),
              TableBootstrap("b", "TESTLIB", "QDC_LINES", boundary(4250)))
    env = _container_env(build_reader_deployment(_reader_spec(tables=tables)))
    assert json.loads(env["AS400_CAPTURE_BINDING_JSON"]) == BINDING.to_dict()
    assert JournalBoundary.from_dict(json.loads(env["AS400_BOUNDARY_JSON"])).require_bound() == POINT


def test_shared_reader_rejects_tables_from_different_epochs():
    from quadringent_control_plane.v2.executor.manifests import ManifestError, TableBootstrap

    foreign_point = replace(POINT, capture_epoch="other-epoch")
    foreign_binding = replace(BINDING, capture_epoch="other-epoch", bootstrap=foreign_point)
    foreign = JournalBoundary("TESTLIB", "RCV0001", 4250, NOW,
                              receiver_identity=IDENTITY).with_binding(foreign_binding)
    tables = (TableBootstrap("a", "TESTLIB", "QDC_ORDERS", boundary()),
              TableBootstrap("b", "TESTLIB", "QDC_LINES", foreign))
    with pytest.raises(ManifestError):
        build_reader_deployment(_reader_spec(tables=tables))


@pytest.fixture()
def executor_case():
    from sqlalchemy import create_engine
    from quadringent_control_plane.v2 import schema
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorConfig, KubernetesPipelineExecutor

    engine = create_engine("sqlite://")
    schema.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "org", "name": "Offline"})
        connection.execute(schema.sources.insert(), {"id": "src-1", "org_id": "org", "display_name": "Offline",
                           "ibmi_host": "offline.invalid", "ibmi_user": "OFFLINE", "secret_ciphertext": "offline",
                           "detected_timezone": "UTC"})
        connection.execute(schema.destinations.insert(), {"id": "dst-1", "org_id": "org", "snowflake_account": "offline",
                           "key_pair_ciphertext": "offline", "setup_script": "offline"})
        connection.execute(schema.tables.insert(), {"id": "tbl-orders", "source_id": "src-1", "schema_name": "TESTLIB",
                           "table_name": "QDC_ORDERS", "journal_library": "TESTLIB", "journal_name": "TESTJRN"})
        connection.execute(schema.pipelines.insert(), {"id": "pipe-1", "table_id": "tbl-orders", "destination_id": "dst-1"})

    class Jobs:
        def __init__(self):
            self.created = {}
            self.fail = False

        def create_job(self, manifest):
            if self.fail:
                from quadringent_control_plane.k8s_jobs import JobsApiError
                raise JobsApiError("executor_unavailable", "offline create refused")
            self.created[manifest["metadata"]["name"]] = manifest

        def read_job(self, name):
            return self.created.get(name)

    class Deployments:
        def read_deployment(self, name):
            return None

        def create_deployment(self, manifest):
            pass

    class BoundaryReader:
        sequence = 4200
        calls = 0

        def read_boundary(self, **kwargs):
            self.calls += 1
            return JournalBoundary("TESTLIB", "RCV0001", self.sequence, NOW, receiver_identity=IDENTITY)

    storage = MemoryCaptureStorage()
    jobs = Jobs()
    reader = BoundaryReader()
    spec = _reader_spec()
    config = ExecutorConfig(namespace="offline", reader_image="offline", copy_image="offline", replay_image="offline",
                            raw_prefix_root=spec.raw_prefix, storage_backend=spec.storage_backend,
                            service_account_name="offline", raw_bucket=spec.raw_bucket, checkpoint_location="offline-state")
    evidence_reader = type("Evidence", (), {"read": lambda self, key: None})()
    executor = KubernetesPipelineExecutor(engine, jobs_client=jobs, deployments_client=Deployments(),
                                           boundary_reader=reader, evidence_reader=evidence_reader,
                                           config=config, storage=storage)
    yield executor, storage, jobs, reader
    engine.dispose()


def test_binding_and_copy_intent_survive_job_creation_failure_without_new_epoch(executor_case):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, storage, jobs, reader = executor_case
    context = executor._load_context("pipe-1")
    jobs.fail = True
    with pytest.raises(ExecutorError):
        executor.execute(pipeline_id="pipe-1", event="start")
    registry, scope = executor._registry(context)
    original = registry.load_binding(scope)
    assert original.bootstrap.sequence == 4200
    assert any("copy-intents-v1" in key for key in storage.objects)
    reader.sequence = 4250
    jobs.fail = False
    executor.execute(pipeline_id="pipe-1", event="start")
    assert registry.load_binding(scope) == original
    env = _container_env(next(iter(jobs.created.values())))
    assert JournalBoundary.from_dict(json.loads(env["AS400_BOUNDARY_JSON"])).last_sequence == 4250
    assert json.loads(env["AS400_CAPTURE_BINDING_JSON"])["bootstrap"]["sequence"] == 4200


@pytest.mark.parametrize("legacy", [True, False])
def test_existing_legacy_checkpoint_or_raw_never_creates_copy(executor_case, legacy):
    from quadringent.contract import JournalPosition
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, storage, jobs, reader = executor_case
    if legacy:
        storage.checkpoint_store(SCOPE).record = JournalPosition("RCV0001", 4200)
    else:
        storage.receipts.append("receipts/legacy.json")
    with pytest.raises(ExecutorError):
        executor.execute(pipeline_id="pipe-1", event="start")
    assert not jobs.created
    assert not storage.objects
    if legacy:
        assert reader.calls == 0


def test_native_journal_outside_copy_scope_is_refused_before_registry_write(executor_case):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, storage, jobs, _reader = executor_case
    context = executor._load_context("pipe-1")
    foreign = replace(IDENTITY, journal_library="OTHERLIB")
    observed = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=foreign)
    with pytest.raises(ExecutorError):
        executor._admit_boundary(context, observed)
    assert not storage.objects
    assert not jobs.created


@pytest.mark.parametrize("damage", ["absent", "pipeline", "table", "run", "boundary", "extra", "duplicate"])
def test_copy_job_requires_its_exact_durable_intention_before_source(monkeypatch, damage):
    import quadringent_initial_copy_job as copy_job
    from quadringent.receiver_registry import ReceiverNameRegistry

    env = _container_env(build_initial_copy_job(_copy_spec(boundary=boundary())))
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    config = copy_job.InitialCopyConfig.from_environment(env)
    storage = MemoryCaptureStorage()
    store = storage.object_store("receiver-authority-v1")
    registry = ReceiverNameRegistry(store, authority=BINDING.registry_authority, source_system="ibmi", journal="TESTJRN")
    admitted = registry.create_or_load_binding(scope_id=SCOPE, identity=IDENTITY, sequence=4200)
    admitted_boundary = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY).with_binding(admitted)
    config = replace(config, boundary=admitted_boundary)
    intent = {"format_version": "quadringent-copy-intent-v1", "pipeline_id": config.pipeline_id,
              "table_id": config.table_id, "run_id": config.run_id, "boundary": admitted_boundary.to_dict()}
    if damage in {"pipeline", "table", "run"}:
        intent[damage + "_id"] = "foreign"
    elif damage == "boundary":
        intent["boundary"] = replace(admitted_boundary, last_sequence=4210,
                                    bound_checkpoint=admitted.checkpoint(IDENTITY, 4210)).to_dict()
    elif damage == "extra":
        intent["extra"] = True
    key = copy_intent_key(config.pipeline_id, config.table_id, config.run_id)
    if damage != "absent":
        body = json.dumps(intent).encode()
        if damage == "duplicate":
            body = body[:-1] + b', "run_id": ' + json.dumps(config.run_id).encode() + b'}'
        store.put_once(key, body)
    monkeypatch.setattr("quadringent.storage_backend.StorageBackend", lambda *args, **kwargs: storage)
    with pytest.raises(copy_job.InitialCopyJobError, match="intention"):
        copy_job.validate_original_binding(config)


def test_copy_job_admits_a_later_table_intention_without_changing_original_bootstrap(monkeypatch):
    import quadringent_initial_copy_job as copy_job
    from quadringent.receiver_registry import ReceiverNameRegistry

    env = _container_env(build_initial_copy_job(_copy_spec(boundary=boundary(4250))))
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    config = copy_job.InitialCopyConfig.from_environment(env)
    storage = MemoryCaptureStorage()
    store = storage.object_store("receiver-authority-v1")
    registry = ReceiverNameRegistry(store, authority=BINDING.registry_authority, source_system="ibmi", journal="TESTJRN")
    admitted = registry.create_or_load_binding(scope_id=SCOPE, identity=IDENTITY, sequence=4200)
    admitted_boundary = JournalBoundary("TESTLIB", "RCV0001", 4250, NOW, receiver_identity=IDENTITY).with_binding(admitted)
    registry.claim(admitted, admitted_boundary.require_bound())
    config = replace(config, boundary=admitted_boundary)
    store.put_once(copy_intent_key(config.pipeline_id, config.table_id, config.run_id), json.dumps({
        "format_version": "quadringent-copy-intent-v1", "pipeline_id": config.pipeline_id,
        "table_id": config.table_id, "run_id": config.run_id, "boundary": admitted_boundary.to_dict()}).encode())
    monkeypatch.setattr("quadringent.storage_backend.StorageBackend", lambda *args, **kwargs: storage)
    copy_job.validate_original_binding(config)
    assert admitted.bootstrap.sequence == 4200
    assert config.boundary.last_sequence == 4250


@pytest.mark.parametrize("foreign", ["pipeline", "table", "run", "sequence", "epoch"])
def test_original_copy_evidence_is_recoupled_before_promotion(executor_case, foreign):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, _storage, jobs, _reader = executor_case
    executor.execute(pipeline_id="pipe-1", event="start")
    env = _container_env(next(iter(jobs.created.values())))
    original = JournalBoundary.from_dict(json.loads(env["AS400_BOUNDARY_JSON"]))
    run_id = env["AS400_SNAPSHOT_RUN_ID"]
    evidence = InitialCopyEvidence("pipe-1", "tbl-orders", run_id, original, 1, NOW)
    if foreign in {"pipeline", "table", "run"}:
        evidence = replace(evidence, **{foreign + "_id": "foreign"})
    elif foreign == "sequence":
        evidence = replace(evidence, boundary=replace(original, last_sequence=4251,
                           bound_checkpoint=original.capture_binding.checkpoint(IDENTITY, 4251)))
    else:
        point = replace(original.bound_checkpoint, capture_epoch="foreign")
        binding = replace(original.capture_binding, capture_epoch="foreign", bootstrap=point)
        evidence = replace(evidence, boundary=replace(original, capture_binding=binding, bound_checkpoint=point))
    executor._evidence_reader = type("Evidence", (), {"read": lambda self, key: evidence})()
    jobs.created.clear()
    with pytest.raises(ExecutorError):
        executor.copying_evidence("pipe-1", run_id)


def test_reader_rejects_missing_run_without_tail_fallback(executor_case):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, _storage, _jobs, reader = executor_case
    with pytest.raises(ExecutorError, match="original bound copy run"):
        executor._reader_boundary("src-1", {"active_run_id": None})
    assert reader.calls == 0


def test_rendered_reader_environment_is_admitted_by_the_real_entrypoint():
    import as400_continuous_capture as capture
    from quadringent_control_plane.v2.executor.manifests import TableBootstrap

    env = _container_env(build_reader_deployment(_reader_spec(
        tables=(TableBootstrap("a", "TESTLIB", "QDC_ORDERS", boundary()),))))
    assert capture._installed_capture_binding(env) == BINDING


@pytest.mark.parametrize("failure", ["before", "after"])
def test_native_generation_changed_during_copy_prevents_publication(monkeypatch, failure):
    import quadringent_initial_copy_job as copy_job
    from quadringent.continuous import ReceiverSnapshot

    env = _container_env(build_initial_copy_job(_copy_spec(boundary=boundary())))
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    stages = []
    monkeypatch.setattr(copy_job, "validate_original_binding", lambda config: stages.append("registry"))
    monkeypatch.setattr(copy_job, "run_snapshot", lambda *args, **kwargs: stages.append("snapshot") or 1)
    monkeypatch.setattr(copy_job, "publish_snapshot", lambda *args, **kwargs: stages.append("publish") or [])
    monkeypatch.setattr(copy_job, "write_evidence", lambda *args, **kwargs: stages.append("evidence"))

    class Catalog:
        calls = 0

        def identity(self, **kwargs):
            Catalog.calls += 1
            if failure == "before" or Catalog.calls > 2:
                return replace(IDENTITY, native_generation="different-generation")
            return IDENTITY

        def snapshot(self, required_receiver=None):
            return [ReceiverSnapshot("TESTLIB", "RCV0001", 1, 4300, "ATTACHED")]

    with pytest.raises(copy_job.InitialCopyJobError):
        copy_job.main([], environ=env, catalog_factory=lambda **kwargs: Catalog())
    assert "publish" not in stages
    assert "evidence" not in stages
    assert ("snapshot" in stages) == (failure == "after")


@pytest.mark.parametrize("status", ["ATTACHED", "ONLINE", "SAVED", "DETACHED", "OFFLINE", "PURGED", "UNKNOWN"])
def test_initial_copy_revalidates_readable_native_storage_before_publication(status):
    import quadringent_initial_copy_job as copy_job
    from quadringent.continuous import ReceiverSnapshot

    env = _container_env(build_initial_copy_job(_copy_spec(boundary=boundary())))
    env.update(ISERIES_PASSWORD="offline-only", AS400_JAVA_CLASSPATH="offline.jar")
    config = copy_job.InitialCopyConfig.from_environment(env)

    class Catalog:
        def identity(self, **kwargs):
            return IDENTITY

        def snapshot(self, required_receiver=None):
            return [ReceiverSnapshot("TESTLIB", "RCV0001", 1, 4300, status)]

    if status in {"ATTACHED", "ONLINE", "SAVED"}:
        copy_job.validate_native_boundary(config, base_environ=env, catalog_factory=lambda **kwargs: Catalog())
    else:
        with pytest.raises(copy_job.InitialCopyJobError):
            copy_job.validate_native_boundary(config, base_environ=env, catalog_factory=lambda **kwargs: Catalog())
