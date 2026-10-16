"""Authority is SQL-owned; matching local streams are not native admission."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event, Thread

from alembic import command
import pytest
from sqlalchemy import select

from quadringent_control_plane.v2 import db, schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.native_context import (
    NativeContextConflict, context_transaction, external_operation,
)
from quadringent_control_plane.v2.services.native_verifications import (
    NativeAdmission, NativeArtifact, NativePreparation, NativeVerificationsService,
)
from quadringent_control_plane.v2.services.sources import SourcesService
from quadringent_control_plane.v2.services.tables import TablesService
from quadringent_control_plane.v2.services.destinations import DestinationsService
from quadringent_control_plane.v2.services.pipelines import PipelinesService
from test_native_verification_ledger import layers, run_all, scope
from test_v2_executor_kubernetes import executor_env  # noqa: F401

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def authority(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'authority.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    with engine.begin() as c:
        for org in ("org-one", "org-other"):
            c.execute(schema.organizations.insert(), {"id": org, "name": "Fixture",
                                                       "native_context_enforced": True})
        for suffix, org in (("one", "org-one"), ("two", "org-one"), ("other", "org-other")):
            c.execute(schema.sources.insert(), {
                "id": f"source-{suffix}", "org_id": org, "display_name": "Fixture",
                "ibmi_host": "source.invalid", "ibmi_user": "FIXTURE", "secret_ciphertext": "x",
            })
            c.execute(schema.destinations.insert(), {
                "id": f"destination-{suffix}", "org_id": org, "snowflake_account": "fixture",
                "destination_database": "DEST", "destination_schema": "TEST",
                "key_pair_ciphertext": "x", "setup_script": "--",
            })
            c.execute(schema.tables.insert(), {
                "id": f"table-{suffix}", "source_id": f"source-{suffix}", "schema_name": "TESTLIB",
                "table_name": "ITEMS", "readiness": "ready", "key_strategy": "primary",
                "key_columns": "ID", "journal_library": "TESTLIB", "journal_name": "JRN",
                "journal_status": "journaled", "images": "*BOTH",
                "discovered_columns": [{"name": "ID", "kind": "integer"},
                                       {"name": "LABEL", "kind": "varchar", "length": 40},
                                       {"name": "AMOUNT", "kind": "decimal", "precision": 38, "scale": 0}],
            })
            c.execute(schema.pipelines.insert(), {
                "id": f"pipeline-{suffix}", "table_id": f"table-{suffix}",
                "destination_id": f"destination-{suffix}", "active_run_id": f"run-{suffix}",
                "declared_state": "live",
            })
    yield engine, tmp_path
    engine.dispose()


def artifact(name):
    return NativeArtifact(name, "s3://synthetic-proof/" + name.replace(":", "-"),
                          "a" * 64, "version-one", 100)


class Preparer:
    def prepare(self, context):
        return NativePreparation(scope(), tuple(artifact(n) for n in (
            "copy_intent", "copy_evidence", "registry", "binding", "checkpoint",
        )))


class Controller:
    def __init__(self, directory):
        self.directory = directory
        self.calls = 0

    def admit(self, prepared):
        self.calls += 1
        s = scope()
        (self.directory / f"ledger-{self.calls}").mkdir()
        report = run_all(self.directory / f"ledger-{self.calls}", s, layers(s))
        refs = tuple(NativeArtifact("stream:" + name, "s3://synthetic-proof/stream-" + name,
                                   report["datasets"][name]["sha256"], "version-one",
                                   report["datasets"][name]["byte_count"]) for name in layers(s)) + tuple(
            artifact(n) for n in ("platform", "source", "raw_inventory", "native_format",
                                  "receiver_chain", "destination", "stable_cut"))
        return NativeAdmission(report, refs, "offline-test-policy-v1")


def service(authority, **kw):
    return NativeVerificationsService(authority[0], org_id="org-one", now=lambda: NOW, **kw)


def running(authority):
    api = service(authority, preparer=Preparer(), controller=Controller(authority[1]))
    requested = api.request("pipeline-one", requested_by="fixture", idempotency_key="first")
    lease = api.claim(requested["id"], holder="controller-one")
    lease = api.prepare(lease)
    return api, lease


def row(authority, pipeline="pipeline-one"):
    with authority[0].connect() as c:
        return c.execute(select(schema.pipelines).where(schema.pipelines.c.id == pipeline)).mappings().one()


def test_migration_existing_destinations_and_default_authority(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'legacy.sqlite3'}"
    command.upgrade(db.alembic_config(dsn), "0016_destination_scope")
    engine = db.create_engine_for(dsn)
    with engine.begin() as c:
        c.execute(schema.organizations.insert(), {"id": "legacy", "name": "Fixture"})
        c.execute(schema.destinations.insert(), {"id": "legacy-dest", "org_id": "legacy",
            "snowflake_account": "fixture", "destination_database": "ORIGINAL",
            "destination_schema": "ISOLATED", "key_pair_ciphertext": "x", "setup_script": "--"})
        c.execute(schema.sources.insert(), {"id": "legacy-source", "org_id": "legacy", "display_name": "Fixture",
            "ibmi_host": "fixture.invalid", "ibmi_user": "FIXTURE", "secret_ciphertext": "x"})
        c.execute(schema.tables.insert(), {"id": "legacy-table", "source_id": "legacy-source",
            "schema_name": "TESTLIB", "table_name": "ITEMS"})
        c.execute(schema.pipelines.insert(), {"id": "legacy-pipeline", "table_id": "legacy-table",
            "destination_id": "legacy-dest", "active_run_id": "original-run"})
    db.run_migrations(dsn)
    db.run_migrations(dsn)
    with engine.connect() as c:
        assert c.execute(select(schema.destinations.c.destination_schema)).scalar_one() == "ISOLATED"
        assert c.execute(select(schema.native_verifications)).all() == []
        assert c.execute(select(schema.organizations.c.native_context_enforced)
                         .where(schema.organizations.c.id == "legacy")).scalar_one() is False
        migrated = c.execute(select(schema.pipelines)).mappings().one()
        assert migrated["active_run_id"] == "original-run"
        assert migrated["native_context_generation"] == 0
        assert migrated["native_context_operation_id"] is None
        assert migrated["latest_native_verification_id"] is None
    engine.dispose()


def test_request_is_immutable_idempotent_and_advances_current_pointer(authority):
    api = service(authority)
    first = api.request("pipeline-one", requested_by="fixture", idempotency_key="same")
    assert api.request("pipeline-one", requested_by="fixture", idempotency_key="same")["id"] == first["id"]
    second = api.request("pipeline-one", requested_by="fixture", idempotency_key="next")
    assert api.get_latest("pipeline-one")["id"] == second["id"]
    assert api.get(first["id"])["context_validity"] == "superseded"


def test_default_preparation_and_admission_fail_closed(authority):
    api = service(authority)
    attempt = api.request("pipeline-one", requested_by="fixture", idempotency_key="closed")
    lease = api.claim(attempt["id"], holder="controller-one")
    api.prepare(lease)
    result = api.get_latest("pipeline-one")
    assert result["outcome"] == "incomplete"
    assert result["reason_code"] == "preparation_unavailable"
    assert result["result_manifest"] is None


def test_seal_persists_real_local_comparison_but_only_with_internal_test_port(authority):
    api, lease = running(authority)
    result = api.finalize(lease)
    assert result["outcome"] == "verified"
    assert result["context_validity"] == "same_context"
    assert result["head_freshness"] == "unknown"
    assert result["result_manifest"]["comparison"]["transaction_consistency_verified"] is False
    assert api.finalize(lease) == result
    assert api.controller.calls == 1
    restarted = service(authority)
    assert restarted.get_latest("pipeline-one") == result
    json.dumps(result)


def test_new_pending_or_incomplete_attempt_never_falls_back_to_old_green(authority):
    api, lease = running(authority)
    old = api.finalize(lease)
    new = api.request("pipeline-one", requested_by="fixture", idempotency_key="new")
    assert api.get_latest("pipeline-one")["outcome"] is None
    api.close_incomplete(api.claim(new["id"], holder="controller-two"), reason_code="collection_failed")
    assert api.get_latest("pipeline-one")["outcome"] == "incomplete"
    assert api.get(old["id"])["outcome"] == "verified"
    assert api.get(old["id"])["context_validity"] == "superseded"


@pytest.mark.parametrize("forged", [True, False])
def test_dict_passed_or_ledger_alone_cannot_publish(authority, forged):
    api, lease = running(authority)
    class BadController:
        def admit(self, prepared):
            if forged:
                return {"passed": True, "state": "verified"}
            return NativeAdmission({"state": "verified"}, (), "offline-test-policy-v1")
    api.controller = BadController()
    assert api.finalize(lease)["outcome"] == "incomplete"


def test_final_cas_refuses_aba_on_real_key_writer(authority):
    api, lease = running(authority)
    tables = TablesService(authority[0], org_id="org-one")
    tables.choose_key("table-one", key_strategy="unique_index", key_columns=["ID"])
    tables.choose_key("table-one", key_strategy="primary", key_columns=["ID"])
    assert row(authority)["native_context_generation"] == 2
    with pytest.raises(NativeContextConflict):
        api.finalize(lease)
    assert api.get_latest("pipeline-one")["context_validity"] == "superseded"


def test_lost_or_expired_controller_lease_cannot_finalize(authority):
    api, lease = running(authority)
    api.now = lambda: NOW + timedelta(hours=1)
    replacement = api.claim(lease.request_id, holder="controller-two")
    with pytest.raises(NativeContextConflict):
        api.finalize(lease)
    assert replacement.lease_generation > lease.lease_generation


def test_external_effect_is_reserved_before_call_and_ambiguous_failure_is_durable(authority):
    api = service(authority)
    with pytest.raises(RuntimeError):
        with external_operation(authority[0], ("pipeline-one",)):
            pending = row(authority)
            assert pending["native_context_generation"] == 1
            assert pending["native_context_operation_id"]
            with pytest.raises(NativeContextConflict):
                api.request("pipeline-one", requested_by="fixture", idempotency_key="during")
            raise RuntimeError("provider outcome unknown")
    assert row(authority)["native_context_operation_id"]
    with pytest.raises(NativeContextConflict):
        api.request("pipeline-one", requested_by="fixture", idempotency_key="after")


def test_pipeline_action_closes_fence_without_network_lock(authority):
    engine, _ = authority
    class Executor:
        def execute(self, **values):
            assert row(authority)["native_context_operation_id"]
            with engine.begin() as c:
                c.execute(schema.pipelines.update().where(schema.pipelines.c.id == "pipeline-two")
                          .values(attention_reason="Unrelated bookkeeping"))
    PipelinesService(engine, org_id="org-one").apply_action("pipeline-one", "pause", executor=Executor())
    assert row(authority)["native_context_generation"] == 1
    assert row(authority)["native_context_operation_id"] is None


def test_pause_noops_and_columns_only_invalidate_affected_context(authority):
    engine, _ = authority
    secret = SecretBox(SecretBox.generate_key())
    sources = SourcesService(engine, secret, org_id="org-one")
    sources.pause("source-one")
    sources.pause("source-one")
    assert row(authority)["native_context_generation"] == 1
    sources.resume("source-one")
    assert row(authority)["native_context_generation"] == 2
    destinations = DestinationsService(engine, secret, org_id="org-one")
    destinations.pause("destination-one")
    destinations.pause("destination-one")
    assert row(authority)["native_context_generation"] == 3
    assert row(authority, "pipeline-two")["native_context_generation"] == 0


@pytest.mark.parametrize("pipeline,restrictions", [
    ("pipeline-other", ()), ("pipeline-one", ("source-two",)),
])
def test_other_org_or_source_cannot_request_or_read(authority, pipeline, restrictions):
    api = service(authority, source_restriction=restrictions)
    with pytest.raises(LookupError):
        api.request(pipeline, requested_by="fixture", idempotency_key="outside")


def test_sqlite_mutex_serializes_independent_connections(authority):
    started, done = Event(), Event()
    failures = []
    def competitor():
        started.set()
        try:
            with context_transaction(authority[0], "org-one"):
                done.set()
        except Exception as exc:
            failures.append(exc)
    with context_transaction(authority[0], "org-one"):
        thread = Thread(target=competitor)
        thread.start()
        assert started.wait(1)
        assert not done.wait(0.05)
    thread.join(3)
    assert done.is_set() and not failures



@pytest.mark.parametrize("component", ["reader", "loader"])
def test_real_workload_reconcile_reserves_shared_fence_only_for_actual_changes(authority, monkeypatch, component):
    from copy import deepcopy
    from types import SimpleNamespace
    from quadringent_control_plane.v2.executor.kubernetes import KubernetesPipelineExecutor
    executor = object.__new__(KubernetesPipelineExecutor)
    executor._engine = authority[0]
    executor._config = SimpleNamespace(storage_backend="gcs", aws_region=None, loader_image="fixture")
    with authority[0].begin() as c:
        c.execute(schema.tables.insert(), {"id": "table-shared", "source_id": "source-one" if component == "reader"
            else "source-two", "schema_name": "TESTLIB", "table_name": "SHARED", "journal_library": "TESTLIB",
            "journal_name": "JRN"})
        c.execute(schema.pipelines.insert(), {"id": "pipeline-shared", "table_id": "table-shared",
            "destination_id": "destination-two" if component == "reader" else "destination-one"})
    desired = {"metadata": {"name": "fixture", "namespace": "fixture"}, "spec": {"replicas": 1}}
    class Deployments:
        observed = None
        def read_deployment(self, name):
            return deepcopy(self.observed)
        def create_deployment(self, manifest):
            assert row(authority)["native_context_operation_id"]
            assert row(authority, "pipeline-shared")["native_context_operation_id"]
            self.observed = deepcopy(manifest)
        def replace_deployment(self, name, manifest):
            assert row(authority)["native_context_operation_id"]
            self.observed = deepcopy(manifest)
        def delete_deployment(self, name):
            assert row(authority)["native_context_operation_id"]
            self.observed = None
    executor._deployments = Deployments()
    if component == "reader":
        monkeypatch.setattr(executor, "_desired_reader_manifests", lambda *a, **kw: {("TESTLIB", "JRN"): desired})
        monkeypatch.setattr(executor, "_known_journals", lambda *a: set())
        action = lambda: executor._reconcile_reader_for_source("source-one")
    else:
        monkeypatch.setattr(executor, "_desired_loader_manifest", lambda *a, **kw: desired)
        action = lambda: executor._reconcile_loader_for_destination("destination-one")
    action()
    assert row(authority)["native_context_generation"] == 1
    assert row(authority, "pipeline-shared")["native_context_generation"] == 1
    assert row(authority)["native_context_operation_id"] is None
    action()
    assert row(authority)["native_context_generation"] == 1
    assert row(authority, "pipeline-two")["native_context_generation"] == 0


def test_direct_executor_replay_is_noop_but_start_has_a_durable_fence(authority, monkeypatch):
    from quadringent_control_plane.v2.executor.kubernetes import KubernetesPipelineExecutor
    executor = object.__new__(KubernetesPipelineExecutor)
    executor._engine = authority[0]
    class Context:
        declared_state = "not_started"
    monkeypatch.setattr(executor, "_load_context", lambda _: Context())
    def start(*args, **kwargs):
        assert row(authority)["native_context_operation_id"]
        raise RuntimeError("ambiguous network effect")
    monkeypatch.setattr(executor, "_start", start)
    executor.execute(pipeline_id="pipeline-one", event="replay")
    assert row(authority)["native_context_generation"] == 0
    with pytest.raises(RuntimeError):
        executor.execute(pipeline_id="pipeline-one", event="start")
    assert row(authority)["native_context_operation_id"]


def test_preparation_and_finalization_recheck_context_after_outside_work(authority):
    api = service(authority, controller=Controller(authority[1]))
    class ChangedPreparer(Preparer):
        def prepare(self, context):
            SourcesService(authority[0], SecretBox(SecretBox.generate_key()), org_id="org-one").pause("source-one")
            return super().prepare(context)
    api.preparer = ChangedPreparer()
    attempt = api.request("pipeline-one", requested_by="fixture", idempotency_key="preparation-race")
    lease = api.claim(attempt["id"], holder="fixture")
    with pytest.raises(NativeContextConflict):
        api.prepare(lease)
    assert api.get(attempt["id"])["scope_sha256"] is None


def test_pending_effect_cannot_be_cleared_by_elapsed_time_or_a_boolean(authority):
    from quadringent_control_plane.v2.services.native_context import resolve_operation
    with pytest.raises(RuntimeError):
        with external_operation(authority[0], ("pipeline-one",)):
            raise RuntimeError("unknown effect")
    token = row(authority)["native_context_operation_id"]
    with pytest.raises(NativeContextConflict):
        resolve_operation(authority[0], org_id="org-one", token=token)
    class BooleanObserver:
        def observe(self, operation_id, contexts):
            return True
    with pytest.raises(NativeContextConflict):
        resolve_operation(authority[0], org_id="org-one", token=token, observer=BooleanObserver())
    assert row(authority)["native_context_operation_id"] == token


def test_originals_hash_is_bound_to_each_framed_stream_summary(authority):
    api, lease = running(authority)
    controller = api.controller
    class WrongStream:
        def admit(self, prepared):
            admitted = controller.admit(prepared)
            refs = tuple(replace(ref, sha256="b" * 64) if ref.name == "stream:source" else ref
                         for ref in admitted.artifacts)
            return replace(admitted, artifacts=refs)
    api.controller = WrongStream()
    assert api.finalize(lease)["outcome"] == "incomplete"


def test_get_never_trusts_a_changed_sql_manifest_or_unknown_version(authority):
    api, lease = running(authority)
    result = api.finalize(lease)
    with authority[0].begin() as c:
        c.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                  .values(result_manifest_json='{"passed":true}'))
    read = service(authority).get_latest("pipeline-one")
    assert read["outcome"] == "incomplete"
    assert read["result_manifest"] is None
    assert read["reason_code"] == "stored_result_invalid"


def test_scope_and_admission_ports_are_unavailable_independently(authority):
    api = service(authority, preparer=Preparer())
    request = api.request("pipeline-one", requested_by="fixture", idempotency_key="no-admission")
    lease = api.prepare(api.claim(request["id"], holder="controller"))
    result = api.finalize(lease)
    assert result["outcome"] == "incomplete" and result["reason_code"] == "admission_unavailable"


def test_lease_renewal_preserves_the_inflight_revision_and_cannot_revive_expiry(authority):
    api, lease = running(authority)
    api.now = lambda: NOW + timedelta(seconds=200)
    assert api.renew(lease) == lease
    api.now = lambda: NOW + timedelta(seconds=400)
    assert api.finalize(lease)["outcome"] == "verified"


def test_expired_lease_cannot_be_renewed(authority):
    api, lease = running(authority)
    api.now = lambda: NOW + timedelta(hours=1)
    with pytest.raises(NativeContextConflict):
        api.renew(lease)


def test_recovery_requires_observation_and_retains_its_pinned_reference(authority):
    import hashlib
    from quadringent_control_plane.v2.services.native_context import (
        OperationObservation, canonical, resolve_operation,
    )
    with pytest.raises(RuntimeError):
        with external_operation(authority[0], ("pipeline-one",)):
            raise RuntimeError("unknown effect")
    token = row(authority)["native_context_operation_id"]
    class Observer:
        def observe(self, operation_id, contexts):
            with authority[0].begin() as c:
                c.execute(schema.pipelines.update().where(schema.pipelines.c.id == "pipeline-two")
                          .values(attention_reason="No lock around observation"))
            return OperationObservation(operation_id, hashlib.sha256(canonical(contexts).encode()).hexdigest(),
                                        artifact("operation_observation"))
    resolve_operation(authority[0], org_id="org-one", token=token, observer=Observer())
    current = row(authority)
    assert current["native_context_operation_id"] is None
    assert current["native_context_generation"] == 1
    assert json.loads(current["native_context_observation_json"])["operation_id"] == token


def test_nested_effect_cannot_continue_after_a_semantic_writer_loses_its_fence(authority):
    with pytest.raises(NativeContextConflict):
        with external_operation(authority[0], ("pipeline-one",)):
            SourcesService(authority[0], SecretBox(SecretBox.generate_key()), org_id="org-one").pause("source-one")
            with external_operation(authority[0], ("pipeline-one",)):
                pytest.fail("No further effect is allowed after the fence is lost")
    assert row(authority)["native_context_operation_id"]


def test_preparer_cannot_choose_a_subset_of_the_declared_business_columns(authority):
    class SubsetPreparer(Preparer):
        def prepare(self, context):
            prepared = super().prepare(context)
            return replace(prepared, scope=replace(prepared.scope, columns=prepared.scope.columns[:1]))
    api = service(authority, preparer=SubsetPreparer())
    request = api.request("pipeline-one", requested_by="fixture", idempotency_key="subset")
    api.prepare(api.claim(request["id"], holder="controller"))
    assert api.get_latest("pipeline-one")["outcome"] == "incomplete"


def test_timezone_and_trust_observations_ignore_telemetry_only_updates(authority):
    from quadringent_control_plane.v2.services.source_probe import ProbeOutcome, build_probe_result
    secret = SecretBox(SecretBox.generate_key())
    with authority[0].begin() as c:
        c.execute(schema.sources.update().where(schema.sources.c.id == "source-one")
                  .values(secret_ciphertext=secret.encrypt("synthetic-value")))
    class Probe:
        zone = "Q0000UTC"
        version = "V7R5M0"
        def probe(self, request):
            return build_probe_result(network=ProbeOutcome(True, "ok"), tls=ProbeOutcome(True, "ok"),
                tls_fingerprint="aa", authentication=ProbeOutcome(True, "ok"),
                ibmi_version=self.version, qtimzon=self.zone)
    sources, probe = SourcesService(authority[0], secret, org_id="org-one"), Probe()
    sources.test("source-one", probe=probe)
    generation = row(authority)["native_context_generation"]
    assert generation == 1
    probe.version = "V7R5M1"
    sources.test("source-one", probe=probe)
    assert row(authority)["native_context_generation"] == generation
    probe.zone = "QN0500EST"
    sources.test("source-one", probe=probe)
    assert row(authority)["native_context_generation"] == generation + 1


def test_latest_pointer_moves_before_external_preparation(authority):
    api, lease = running(authority)
    old = api.finalize(lease)
    class InspectPreparer(Preparer):
        def prepare(self, context):
            latest = api.get_latest("pipeline-one")
            assert latest["id"] != old["id"] and latest["outcome"] is None
            assert latest["lifecycle"] == "preparing"
            return super().prepare(context)
    api.preparer = InspectPreparer()
    requested = api.request("pipeline-one", requested_by="fixture", idempotency_key="replacement")
    api.prepare(api.claim(requested["id"], holder="replacement"))


def test_conflicting_concurrent_finalization_loses_atomic_cas(authority):
    api, lease = running(authority)
    admitted, release = Event(), Event()
    old_controller = api.controller
    class SlowController:
        def admit(self, prepared):
            result = old_controller.admit(prepared)
            admitted.set()
            assert release.wait(3)
            return result
    api.controller = SlowController()
    other_directory = authority[1] / "competitor"
    other_directory.mkdir()
    class DifferentController(Controller):
        def admit(self, prepared):
            return replace(super().admit(prepared), policy_revision="offline-test-policy-v2")
    other = service(authority, controller=DifferentController(other_directory))
    failures = []
    def complete():
        try:
            api.finalize(lease)
        except Exception as exc:
            failures.append(exc)
    thread = Thread(target=complete)
    thread.start()
    assert admitted.wait(3)
    winner = other.finalize(lease)
    release.set()
    thread.join(3)
    assert len(failures) == 1 and isinstance(failures[0], NativeContextConflict)
    assert api.get_latest("pipeline-one")["result_sha256"] == winner["result_sha256"]


def test_declared_columns_preserve_order_and_precision_invalidation(authority):
    tables = TablesService(authority[0], org_id="org-one")
    columns = [{"name": "ID", "kind": "integer"},
               {"name": "LABEL", "kind": "varchar", "length": 40},
               {"name": "AMOUNT", "kind": "decimal", "precision": 38, "scale": 0}]
    tables.set_discovered_columns("table-one", columns=columns)
    first = row(authority)["native_context_generation"]
    tables.set_discovered_columns("table-one", columns=columns)
    assert row(authority)["native_context_generation"] == first
    tables.set_discovered_columns("table-one", columns=[columns[0], columns[2], columns[1]])
    assert row(authority)["native_context_generation"] == first + 1
    columns[2]["precision"] = 30
    tables.set_discovered_columns("table-one", columns=columns)
    assert row(authority)["native_context_generation"] == first + 2


def test_counts_only_refresh_does_not_invalidate_native_context(authority):
    from quadringent.table_discovery import DiscoveredTable
    class Discovery:
        count = 20
        def discover(self, **kwargs):
            return (DiscoveredTable(library="TESTLIB", system_name="ITEMS", sql_name="ITEMS", text=None,
                row_count=self.count, size_bytes=self.count * 100, has_key=True, key_columns=("ID",),
                journaled=True, journal_library="TESTLIB", journal_name="JRN", images="*BOTH", omitted=False),)
    tables, discovery = TablesService(authority[0], org_id="org-one"), Discovery()
    tables.refresh("source-one", discovery_client=discovery)
    assert row(authority)["native_context_generation"] == 0
    discovery.count = 40
    tables.refresh("source-one", discovery_client=discovery)
    assert row(authority)["native_context_generation"] == 0


def test_request_rejects_missing_run_and_cross_organization_destination(authority):
    api = service(authority)
    with authority[0].begin() as c:
        c.execute(schema.pipelines.update().where(schema.pipelines.c.id == "pipeline-one")
                  .values(active_run_id=None))
    with pytest.raises(NativeContextConflict):
        api.request("pipeline-one", requested_by="fixture", idempotency_key="missing-run")
    with authority[0].begin() as c:
        c.execute(schema.pipelines.update().where(schema.pipelines.c.id == "pipeline-one")
                  .values(active_run_id="run-one", destination_id="destination-other"))
    with pytest.raises(LookupError):
        api.request("pipeline-one", requested_by="fixture", idempotency_key="foreign-destination")


def test_pending_service_action_uses_the_existing_controlled_executor_error(authority):
    from quadringent_control_plane.v2.services.pipelines import PipelineExecutorUnavailableError
    with pytest.raises(RuntimeError):
        with external_operation(authority[0], ("pipeline-one",)):
            raise RuntimeError("unknown effect")
    class NoEffect:
        def execute(self, **kw):
            pytest.fail("A pending effect must be observed first")
    with pytest.raises(PipelineExecutorUnavailableError):
        PipelinesService(authority[0], org_id="org-one").apply_action(
            "pipeline-one", "pause", executor=NoEffect())


def test_unknown_direct_executor_pipeline_preserves_not_found_contract(authority):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError, KubernetesPipelineExecutor
    executor = object.__new__(KubernetesPipelineExecutor)
    executor._engine = authority[0]
    with pytest.raises(ExecutorError) as error:
        executor.execute(pipeline_id="missing", event="pause")
    assert error.value.code == "not_found"


def test_future_collection_times_cannot_be_sealed_as_current_evidence(authority):
    api, lease = running(authority)
    controller = api.controller
    class FutureController:
        def admit(self, prepared):
            admitted = controller.admit(prepared)
            for summary in admitted.comparison["datasets"].values():
                summary["completed_at"] = "2099-01-01T00:00:00Z"
            return admitted
    api.controller = FutureController()
    assert api.finalize(lease)["outcome"] == "incomplete"


def test_direct_start_requires_fence_before_any_external_effect(executor_env):
    from test_v2_executor_kubernetes import _seed
    engine, jobs, deployments, boundary, executor = executor_env
    _seed(engine, declared_state="not_started")
    with pytest.raises(NativeContextConflict, match="operation_required"):
        executor._start(executor._load_context("ppl1"), override={"ppl1": "copying"})
    assert not jobs.created and not deployments.objects and not boundary.calls
    with engine.connect() as c:
        assert c.execute(select(schema.pipelines.c.active_run_id)).scalar_one() is None
        assert c.execute(select(schema.pipelines.c.native_context_operation_id)).scalar_one() is None


def test_start_with_a_lost_fence_refuses_before_any_external_effect(executor_env):
    from quadringent_control_plane.v2.services.native_context import semantic_update
    from test_v2_executor_kubernetes import _seed
    engine, jobs, deployments, boundary, executor = executor_env
    _seed(engine, declared_state="not_started")
    with pytest.raises(NativeContextConflict, match="operation_fence_lost"):
        with external_operation(engine, ("ppl1",)):
            context = executor._load_context("ppl1")
            with context_transaction(engine, "default") as c:
                semantic_update(c, schema.sources, "src1", {"detected_timezone": "QN0500EST"})
            executor._start(context, override={"ppl1": "copying"})
    assert not jobs.created and not deployments.objects and not boundary.calls


def test_default_copy_accepted_can_reconcile_after_service_restart(executor_env, monkeypatch):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError
    from test_v2_executor_kubernetes import _seed
    engine, jobs, deployments, _, executor = executor_env
    _seed(engine, declared_state="not_started")
    real_reconcile = executor._reconcile_reader_for_source
    def unavailable(*args, **kwargs):
        raise ExecutorError("executor_unavailable", "Synthetic transient deployment outage")
    monkeypatch.setattr(executor, "_reconcile_reader_for_source", unavailable)
    _, after = PipelinesService(engine, org_id="default").start_table_pipeline(
        "tbl1", destination_id="dst1", executor=executor)
    assert after.declared_state == "copying" and len(jobs.created) == 1
    monkeypatch.setattr(executor, "_reconcile_reader_for_source", real_reconcile)
    engine.dispose()
    restarted = db.create_engine_for(str(engine.url))
    try:
        monkeypatch.setattr(executor, "_engine", restarted)
        assert PipelinesService(restarted, org_id="default").get("ppl1").declared_state == "copying"
        executor._reconcile_reader_for_source("src1")
        assert deployments.objects and len(jobs.created) == 1
        with restarted.connect() as c:
            assert c.execute(select(schema.pipelines.c.native_context_operation_id)).scalar_one() is None
    finally:
        restarted.dispose()


def test_default_provider_failure_can_retry_after_service_restart(executor_env, monkeypatch):
    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError
    from quadringent_control_plane.k8s_jobs import JobsApiError
    from test_v2_executor_kubernetes import _seed
    engine, jobs, _, _, executor = executor_env
    _seed(engine, declared_state="not_started")
    create = jobs.create_job
    calls = []
    def fail_once(manifest):
        calls.append(manifest)
        if len(calls) == 1:
            raise JobsApiError("executor_unavailable", "Synthetic provider failure before acceptance")
        return create(manifest)
    monkeypatch.setattr(jobs, "create_job", fail_once)
    with pytest.raises(ExecutorError):
        PipelinesService(engine, org_id="default").start_table_pipeline(
            "tbl1", destination_id="dst1", executor=executor)
    assert not jobs.created
    engine.dispose()
    restarted = db.create_engine_for(str(engine.url))
    try:
        monkeypatch.setattr(executor, "_engine", restarted)
        _, after = PipelinesService(restarted, org_id="default").start_table_pipeline(
            "tbl1", destination_id="dst1", executor=executor)
        assert after.declared_state == "copying" and len(jobs.created) == 1 and len(calls) == 2
        with restarted.connect() as c:
            assert c.execute(select(schema.pipelines.c.native_context_generation)).scalar_one() > 1
            assert c.execute(select(schema.pipelines.c.native_context_operation_id)).scalar_one() is None
    finally:
        restarted.dispose()


def enforcement(authority, enabled):
    from quadringent_control_plane.v2.services.native_context import semantic_update
    with context_transaction(authority[0], "org-one") as c:
        semantic_update(c, schema.organizations, "org-one", {"native_context_enforced": enabled})


@pytest.mark.parametrize("phase", ["prepare", "finalize"])
def test_disabled_enforcement_cannot_be_activated_by_injected_ports(authority, phase):
    enforcement(authority, False)
    class UnavailablePort:
        def prepare(self, context):
            pytest.fail("Disabled authority must not collect")
        def admit(self, prepared):
            pytest.fail("Disabled authority must not admit")
    api = service(authority, preparer=UnavailablePort(), controller=UnavailablePort())
    request = api.request("pipeline-one", requested_by="fixture", idempotency_key="disabled")
    lease = api.claim(request["id"], holder="controller")
    getattr(api, phase)(lease)
    result = service(authority).get_latest("pipeline-one")
    assert result["outcome"] == "incomplete"
    assert result["reason_code"] == "native_authority_unavailable"
    assert result["context_enforcement"] == "unavailable" and result["result_manifest"] is None


def test_disabled_tracking_preserves_history_and_reactivation_does_not_restore_it(authority):
    api, lease = running(authority)
    original = api.finalize(lease)
    enforcement(authority, False)
    disabled = service(authority).get_latest("pipeline-one")
    assert disabled["outcome"] == "verified" and disabled["result_manifest"] == original["result_manifest"]
    assert disabled["context_validity"] == "tracking_unavailable"
    assert disabled["context_enforcement"] == "unavailable"
    assert row(authority)["native_context_generation"] == 1
    enforcement(authority, True)
    restored = service(authority).get_latest("pipeline-one")
    assert restored["outcome"] == "verified" and restored["context_validity"] == "superseded"
    assert row(authority)["native_context_generation"] == 2
    assert restored["result_sha256"] == original["result_sha256"]


def test_default_retry_does_not_clear_an_existing_strict_pending(authority):
    from quadringent_control_plane.v2.services.native_context import operation_update
    with pytest.raises(RuntimeError):
        with external_operation(authority[0], ("pipeline-one",)):
            raise RuntimeError("Unknown strict provider outcome")
    pending = row(authority)["native_context_operation_id"]
    enforcement(authority, False)
    with external_operation(authority[0], ("pipeline-one",)):
        operation_update(authority[0], "pipeline-one", {"attention_reason": "Operational recovery"})
    assert row(authority)["native_context_operation_id"] == pending
    assert row(authority)["attention_reason"] == "Operational recovery"
    assert row(authority)["native_context_generation"] == 3


def test_default_external_effect_still_checks_generation_cas(authority):
    from quadringent_control_plane.v2.services.native_context import operation_update
    enforcement(authority, False)
    with pytest.raises(NativeContextConflict, match="operation_fence_lost"):
        with external_operation(authority[0], ("pipeline-one",)):
            TablesService(authority[0], org_id="org-one").choose_key(
                "table-one", key_strategy="unique_index", key_columns=["ID"])
            operation_update(authority[0], "pipeline-one", {"attention_reason": "Must not persist"})
    assert row(authority)["attention_reason"] is None
