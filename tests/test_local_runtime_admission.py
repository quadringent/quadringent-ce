"""``local_runtime`` boundary and admission mirror the Kubernetes executor.

The capture image has no SQLAlchemy, so ``read_boundary`` and ``admit`` repeat
the sequences of ``JavaBoundaryReader.read_boundary`` and
``KubernetesPipelineExecutor._start``/``_admit_boundary``/``_write_copy_intent``.
These tests drive the real classes and the mirrors over the same fakes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from local_support import LABEL, local_env, make_data_dir
from quadringent.continuous import ReceiverSnapshot
from quadringent.contract import JournalPosition
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.storage_backend import StorageBackend, registry_authority
from quadringent_control_plane.v2.executor import local_runtime as runtime
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.boundary_reader import (
    BoundaryUnavailableError, JavaBoundaryReader, _SourceCredentials, _TableJournal)
from quadringent_control_plane.v2.executor.evidence import copy_intent_key, evidence_key

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
RUN_ID = "0d6a9c2e-1a2b-4c3d-9e0f-1234567890ab"
RAW_PREFIX = "raw/example"


def identity(generation="generation-a", receiver="RCV0001"):
    return ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", receiver, "native-test-v1", generation)


class FakeCatalog:
    """Deterministic catalog; ``script`` can alter what the second read sees."""

    def __init__(self, snapshots=None, identities=None, fresh=None):
        self._snapshots = snapshots if snapshots is not None else [
            ReceiverSnapshot("TESTLIB", "RCV0001", 1, 4200, "ATTACHED")]
        self._identities = list(identities or [identity(), identity()])
        self._fresh = fresh
        self.calls = []

    def snapshot(self, required_receiver=None):
        self.calls.append(("snapshot", required_receiver))
        if required_receiver is not None and self._fresh is not None:
            return self._fresh
        return self._snapshots

    def identity(self, **kwargs):
        self.calls.append(("identity", kwargs["receiver_name"]))
        return self._identities.pop(0)


def factory_for(catalog, seen=None):
    def factory(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return catalog
    return factory


class MirrorReader(JavaBoundaryReader):
    """The real reader with its two database lookups replaced by constants."""

    def _table_journal(self, table_id):
        return _TableJournal("src-1", "TESTLIB", "TESTJRN")

    def _source_credentials(self, source_id):
        return _SourceCredentials("192.0.2.10", "TESTUSER", "offline-password", None)


@pytest.fixture()
def password_file(tmp_path):
    path = tmp_path / "ibmi_password"
    path.write_text("offline-password\n")
    return path


def boundary_env(password_file):
    return {"AS400_JOURNAL_LIBRARY": "TESTLIB", "AS400_JOURNAL_NAME": "TESTJRN", "ISERIES_HOST": "192.0.2.10",
            "ISERIES_USER": "TESTUSER", "AS400_JAVA_CLASSPATH": "offline.jar",
            runtime.PASSWORD_FILE_ENV: str(password_file)}


SCENARIOS = {
    "attached": FakeCatalog(),
    "first-sequence-only": lambda: FakeCatalog([ReceiverSnapshot("TESTLIB", "RCV0001", 7, None, "ATTACHED")]),
    "no-attached": lambda: FakeCatalog([ReceiverSnapshot("TESTLIB", "RCV0001", 1, 5, "ONLINE")]),
    "two-attached": lambda: FakeCatalog([ReceiverSnapshot("TESTLIB", "RCV0001", 1, 5, "ATTACHED"),
                                         ReceiverSnapshot("TESTLIB", "RCV0002", 1, 5, "ATTACHED")]),
    "receiver-rotated": lambda: FakeCatalog(fresh=[ReceiverSnapshot("TESTLIB", "RCV0002", 1, 5, "ATTACHED")]),
    "generation-changed": lambda: FakeCatalog(identities=[identity(), identity("generation-b")]),
    "no-sequence": lambda: FakeCatalog([ReceiverSnapshot("TESTLIB", "RCV0001", None, None, "ATTACHED")]),
    "bad-first": lambda: FakeCatalog([ReceiverSnapshot("TESTLIB", "RCV0001", 0, 5, "ATTACHED")]),
    "catalog-error": None,
}


def build_catalog(name):
    scenario = SCENARIOS[name]
    if name == "attached":
        return FakeCatalog()
    if name == "catalog-error":
        class Broken(FakeCatalog):
            def snapshot(self, required_receiver=None):
                raise RuntimeError("jdbc detail that must not surface")
        return Broken()
    return scenario()


@pytest.mark.parametrize("name", [key for key in SCENARIOS])
def test_boundary_reading_matches_the_real_reader(password_file, name):
    real = MirrorReader(None, None, catalog_factory=factory_for(build_catalog(name)), now=lambda: NOW)
    try:
        expected = real.read_boundary(source_id="src-1", table_id="tbl-1")
    except BoundaryUnavailableError as error:
        with pytest.raises(runtime.LocalRuntimeError) as raised:
            runtime.read_boundary(boundary_env(password_file), catalog_factory=factory_for(build_catalog(name)),
                                  now=lambda: NOW)
        assert str(raised.value) == str(error)
        assert "jdbc detail" not in str(raised.value)
        return
    mirrored = runtime.read_boundary(boundary_env(password_file), catalog_factory=factory_for(build_catalog(name)),
                                     now=lambda: NOW)
    assert mirrored == expected


def test_boundary_reading_asks_the_catalog_for_the_same_things(password_file):
    real_catalog, mirror_catalog = FakeCatalog(), FakeCatalog()
    MirrorReader(None, None, catalog_factory=factory_for(real_catalog), now=lambda: NOW).read_boundary(
        source_id="src-1", table_id="tbl-1")
    runtime.read_boundary(boundary_env(password_file), catalog_factory=factory_for(mirror_catalog), now=lambda: NOW)
    assert mirror_catalog.calls == real_catalog.calls


def test_boundary_reading_passes_the_password_value_and_the_pinned_ca(password_file, tmp_path):
    seen = {}
    environ = dict(boundary_env(password_file), AS400_TLS_CA_FILE="/etc/ssl/pinned.pem")
    runtime.read_boundary(environ, catalog_factory=factory_for(FakeCatalog(), seen), now=lambda: NOW)
    assert seen == {"host": "192.0.2.10", "user": "TESTUSER", "password": "offline-password",
                    "journal_library": "TESTLIB", "journal_name": "TESTJRN", "ca_file": "/etc/ssl/pinned.pem"}
    seen.clear()
    runtime.read_boundary(boundary_env(password_file), catalog_factory=factory_for(FakeCatalog(), seen), now=lambda: NOW)
    assert seen["ca_file"] is None


def test_default_catalog_is_the_java_receiver_catalog(password_file, monkeypatch):
    created = {}

    class Java(FakeCatalog):
        def __init__(self, **kwargs):
            created.update(kwargs)
            super().__init__()

    monkeypatch.setattr("quadringent.java_catalog.JavaReceiverCatalog", Java)
    runtime.read_boundary(dict(boundary_env(password_file), AS400_JAVA="/opt/java/bin/java"), now=lambda: NOW)
    assert created["java"] == "/opt/java/bin/java" and created["classpath"] == "offline.jar"
    assert created["password"] == "offline-password" and created["limit"] == 8


def test_observed_boundary_round_trips_and_is_strict(password_file):
    boundary = runtime.read_boundary(boundary_env(password_file), catalog_factory=factory_for(FakeCatalog()),
                                     now=lambda: NOW)
    document = runtime.observed_boundary_to_dict(boundary)
    assert json.loads(json.dumps(document)) == document
    assert runtime.observed_boundary_from_dict(document) == boundary
    assert document["last_sequence"] == 4200 and document["receiver_name"] == "RCV0001"
    assert "offline-password" not in json.dumps(document)
    for broken in (dict(document, extra=1), dict(document, format_version="other"), dict(document, last_sequence="1"),
                   {key: value for key, value in document.items() if key != "receiver_identity"},
                   dict(document, receiver_name="RCV0009"), dict(document, observed_at="2026-10-13T09:30:00")):
        with pytest.raises(runtime.LocalRuntimeError):
            runtime.observed_boundary_from_dict(broken)


# -- admission ----------------------------------------------------------------------------------


@pytest.fixture()
def world(tmp_path):
    from sqlalchemy import create_engine
    from quadringent_control_plane.v2 import schema
    from quadringent_control_plane.v2.executor.kubernetes import (
        ExecutorConfig, KubernetesPipelineExecutor)

    storage = StorageBackend.from_environment(local_env(make_data_dir(tmp_path)))
    engine = create_engine("sqlite://")
    schema.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "org", "name": "Offline"})
        connection.execute(schema.sources.insert(), {
            "id": "src-1", "org_id": "org", "display_name": "Offline", "ibmi_host": "offline.invalid",
            "ibmi_user": "OFFLINE", "secret_ciphertext": "offline", "detected_timezone": "UTC"})
        connection.execute(schema.destinations.insert(), {
            "id": "dst-1", "org_id": "org", "snowflake_account": "offline", "key_pair_ciphertext": "offline",
            "setup_script": "offline"})
        connection.execute(schema.tables.insert(), {
            "id": "tbl-orders", "source_id": "src-1", "schema_name": "TESTLIB", "table_name": "QDC_ORDERS",
            "journal_library": "TESTLIB", "journal_name": "TESTJRN"})
        connection.execute(schema.pipelines.insert(), {"id": "pipe-1", "table_id": "tbl-orders",
                                                       "destination_id": "dst-1"})

    class LocalExecutor(KubernetesPipelineExecutor):
        """Only the registry identity differs: the executor's own map has no ``local``."""

        def _registry(self, context):
            scope = reader_checkpoint_stream_key(
                storage_backend="local", source_id=context.source_id, journal_library=context.journal_library,
                journal_name=context.journal_name, raw_bucket=self._config.raw_bucket,
                raw_prefix=self._config.raw_prefix_root)
            registry = ReceiverNameRegistry(
                self._capture_storage().object_store("receiver-authority-v1"),
                authority=registry_authority("local", self._config.raw_bucket), source_system="ibmi",
                journal=context.journal_name)
            return registry, scope

    config = ExecutorConfig(namespace="offline", reader_image="offline", copy_image="offline", replay_image="offline",
                            raw_prefix_root=RAW_PREFIX, storage_backend="local", service_account_name="offline",
                            raw_bucket=LABEL)
    executor = LocalExecutor(engine, jobs_client=None, deployments_client=None, boundary_reader=None,
                             evidence_reader=None, config=config, storage=storage)
    yield executor, storage
    engine.dispose()


def request(boundary, **overrides):
    document = {"source_id": "src-1", "pipeline_id": "pipe-1", "table_id": "tbl-orders",
                "journal_library": "TESTLIB", "journal_name": "TESTJRN", "raw_prefix": RAW_PREFIX,
                "boundary": runtime.observed_boundary_to_dict(boundary)}
    document.update(overrides)
    return document


def observed(sequence=4200, ident=None):
    return JournalBoundary("TESTLIB", "RCV0001", sequence, NOW, receiver_identity=ident or identity())


def stored_objects(storage):
    return storage.object_store("").list_keys(1000)


def test_admission_is_identical_to_the_executor_on_the_same_storage(world):
    executor, storage = world
    context = executor._load_context("pipe-1")
    bound = executor._admit_boundary(context, observed())
    executor._write_copy_intent(context, RUN_ID, bound)
    before = stored_objects(storage)
    result = runtime.admit(request(observed(), run_id=RUN_ID), storage)
    # same binding loaded, same claim, byte-identical intent: nothing new was written
    assert stored_objects(storage) == before
    assert result["boundary"] == bound.to_dict()
    assert result["binding"] == bound.capture_binding.to_dict()
    scope = executor._registry(context)[1]
    assert result["stream_key"] == scope
    assert result["run_id"] == RUN_ID
    assert result["evidence_key"] == evidence_key(RAW_PREFIX, "tbl-orders", RUN_ID)
    assert result["raw_prefix_root"] == RAW_PREFIX
    assert (result["table_id"], result["pipeline_id"]) == ("tbl-orders", "pipe-1")
    assert result["binding"]["registry_authority"] == f"local://{LABEL}/receiver-authority-v1"


def test_admission_first_then_the_executor_agrees(world):
    executor, storage = world
    result = runtime.admit(request(observed()), storage)
    context = executor._load_context("pipe-1")
    bound = executor._admit_boundary(context, observed())
    assert bound.to_dict() == result["boundary"]
    executor._write_copy_intent(context, result["run_id"], bound)  # idempotent put_once
    assert executor._copy_intent_boundary(context, result["run_id"]) == bound
    assert executor._require_registered_boundary(context, bound) == bound.capture_binding


def test_original_binding_survives_a_later_admission_with_a_newer_boundary(world):
    _executor, storage = world
    first = runtime.admit(request(observed(4200)), storage)
    second = runtime.admit(request(observed(4250)), storage)
    assert first["run_id"] != second["run_id"]
    assert second["binding"] == first["binding"]
    assert second["binding"]["bootstrap"]["sequence"] == 4200
    assert second["boundary"]["bound_checkpoint"]["sequence"] == 4250
    keys = stored_objects(storage)
    assert sum(key.startswith("receiver-authority-v1/copy-intents-v1/") for key in keys) == 2


def test_admission_writes_the_copy_intent_under_the_registry_authority(world):
    _executor, storage = world
    result = runtime.admit(request(observed(), run_id=RUN_ID), storage)
    raw = storage.object_store("receiver-authority-v1").get_bounded(
        copy_intent_key("pipe-1", "tbl-orders", RUN_ID), 65536)
    assert json.loads(raw) == {"format_version": "quadringent-copy-intent-v1", "pipeline_id": "pipe-1",
                               "table_id": "tbl-orders", "run_id": RUN_ID, "boundary": result["boundary"]}
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()


def _executor_refuses(executor, boundary):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    context = executor._load_context("pipe-1")
    with pytest.raises(ExecutorError):
        executor.execute  # noqa: B018 - documents that execute() would route through _start
        executor._capture_storage().checkpoint_store(executor._registry(context)[1]).load_bound()
        executor._admit_boundary(context, boundary)


@pytest.mark.parametrize("damage", ["legacy-checkpoint", "existing-raw", "foreign-journal"])
def test_refusals_match_the_executor_and_write_nothing(world, damage):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    executor, storage = world
    context = executor._load_context("pipe-1")
    scope = executor._registry(context)[1]
    boundary = observed()
    if damage == "legacy-checkpoint":
        legacy = storage.checkpoint_store(scope)
        legacy.compare_and_set(None, JournalPosition("RCV0001", 4200))
    elif damage == "existing-raw":
        storage.object_store(RAW_PREFIX).put_once("receipts/old.json", b"{}")
    else:
        foreign = ReceiverIdentity("OTHERLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")
        boundary = observed(ident=foreign)
    before = stored_objects(storage)
    with pytest.raises(ExecutorError):
        if damage == "legacy-checkpoint":
            # _start performs this check before reading the boundary
            try:
                storage.checkpoint_store(scope).load_bound()
            except ValueError as error:
                raise ExecutorError("capability_unavailable", "legacy") from error
        executor._admit_boundary(context, boundary)
    with pytest.raises(runtime.LocalRuntimeError):
        try:
            runtime.admit(request(boundary), storage)
        except ValueError as error:  # JournalBoundary refuses a foreign journal before admission starts
            raise runtime.LocalRuntimeError(str(error)) from error
    assert stored_objects(storage) == before


def test_admission_with_an_existing_bound_checkpoint_requires_its_registration(world):
    executor, storage = world
    first = runtime.admit(request(observed(4200)), storage)
    context = executor._load_context("pipe-1")
    checkpoint = BoundCheckpoint(first["stream_key"], first["binding"]["capture_epoch"], identity(), 4210)
    bootstrap = BoundCheckpoint.from_dict(first["binding"]["bootstrap"])
    store = storage.checkpoint_store(first["stream_key"])
    store.compare_and_set_bound(None, bootstrap)
    store.compare_and_set_bound(bootstrap, checkpoint)
    again = runtime.admit(request(observed(4300)), storage)
    assert again["binding"] == first["binding"]
    assert executor._admit_boundary(context, observed(4300)).to_dict() == again["boundary"]


def test_a_different_intent_for_the_same_run_is_refused(world):
    _executor, storage = world
    runtime.admit(request(observed(4200), run_id=RUN_ID), storage)
    with pytest.raises(runtime.LocalRuntimeError, match="different copy intent"):
        runtime.admit(request(observed(4250), run_id=RUN_ID), storage)


def test_admission_requires_the_local_backend_and_a_well_formed_request(world):
    _executor, storage = world
    with pytest.raises(runtime.LocalRuntimeError):
        runtime.admit(request(observed()), StorageBackend(kind="gcs", raw_bucket="bucket-a", state_location="s"))
    good = json.dumps(request(observed()))
    assert runtime._parse_request(good)["pipeline_id"] == "pipe-1"
    for broken in ("{", "[]", json.dumps({**request(observed()), "extra": 1}),
                   json.dumps({key: value for key, value in request(observed()).items() if key != "table_id"}),
                   json.dumps(request(observed(), table_id=" padded")),
                   json.dumps(request(observed(), run_id="not-a-uuid")),
                   json.dumps(request(observed(), pipeline_id=7)), "x" * 70000):
        with pytest.raises(runtime.LocalRuntimeError):
            runtime._parse_request(broken)
