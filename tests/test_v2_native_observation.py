"""SQL observations remain incomplete even when all seven streams agree."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy import select

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.services.native_collection import NativeCollectionResult
from quadringent_control_plane.v2.services.native_context import NativeContextConflict, canonical, semantic_update
from quadringent_control_plane.v2.services.native_replay import NativeArchiveReplay
from quadringent_control_plane.v2.services.native_verifications import NativeArtifact, NativePreparation
from test_native_verification_ledger import layers, run_all, scope
from test_v2_native_verifications import NOW, authority, service  # noqa: F401
from test_v2_native_verification_routes import BASE, attempts, native_api, post, recorded_result  # noqa: F401

MISSING = ("platform", "source_provenance", "receiver_chain", "physical_inventory", "stable_cut")
LABELS = ("destination_identity", "receiver_sampled_ranges", "native_source_observations")
LABEL_REFS = ("destination:history:before", "destination:history:after", "destination:mirror:before",
              "destination:mirror:after", "receiver:before", "receiver:after", "guard:journal_original",
              "guard:final_original", "guard:stdout_original", "native_bundle")


def reference(name, *, digest=None, byte_count=100, preparation=False, org="org-one"):
    digest = digest or hashlib.sha256(name.encode()).hexdigest()
    namespace = "native-preparations-v1" if preparation else "native-collections-v1"
    prefix = namespace + "/" + hashlib.sha256(org.encode()).hexdigest() + "/objects/"
    return NativeArtifact(name, "local://fixture/" + prefix + digest, digest, "sha256:" + digest, byte_count)


class ObservationPreparer:
    def prepare(self, context):
        return NativePreparation(scope(), tuple(reference(name, preparation=True) for name in
            ("copy_intent", "copy_evidence", "registry", "binding", "checkpoint")))


class Observer:
    def __init__(self, directory, *, change=None, during=None):
        self.directory, self.change, self.during = directory, change, during
        self.calls = 0

    def observe(self, prepared, *, context, check_current):
        assert check_current() is True
        self.calls += 1
        path = self.directory / ("observed-ledger-" + str(self.calls))
        path.mkdir()
        report = run_all(path, prepared.scope, layers(prepared.scope))
        refs = prepared.artifacts + tuple(reference("stream:" + name, digest=summary["sha256"],
            byte_count=summary["byte_count"]) for name, summary in report["datasets"].items())
        raw = (canonical(report) + "\n").encode()
        refs += (reference("comparison", digest=hashlib.sha256(raw).hexdigest(), byte_count=len(raw)),)
        refs += tuple(reference(name, digest=hashlib.sha256(b"guard:final_original").hexdigest()
                                if name == "guard:stdout_original" else None) for name in LABEL_REFS)
        manifest = reference("collection_manifest")
        collection = NativeCollectionResult(report, refs + (manifest,), manifest)
        replay = NativeArchiveReplay(deepcopy(report), 7, MISSING, LABELS, collection_manifest=manifest,
            scope_sha256=prepared.scope.sha256, context_sha256=hashlib.sha256(canonical(context).encode()).hexdigest())
        if self.during:
            self.during(context)
        if self.change:
            collection, replay = self.change(collection, replay)
        assert check_current() is True
        return collection, replay


def observed(authority, **kwargs):
    observer = Observer(authority[1], **kwargs)
    api = service(authority, preparer=ObservationPreparer(), observer=observer)
    attempt = api.request("pipeline-one", requested_by="fixture", idempotency_key="observed")
    lease = api.prepare(api.claim(attempt["id"], holder="observer"))
    return api, lease, observer


def test_observation_persists_replayed_contents_without_admission_after_restart(authority):
    api, lease, observer = observed(authority)
    result = api.finalize(lease)
    assert result["outcome"] == "incomplete"
    assert result["reason_code"] == "native_authority_unavailable"
    manifest = result["result_manifest"]
    assert manifest["format_version"] == "quadringent-native-observed-result-v1"
    assert manifest["comparison"]["state"] == "verified"
    assert manifest["native_admission"] == "not_evaluated" and manifest["native_cdc_qualified"] is False
    assert manifest["streams_replayed"] == 7
    assert set(manifest["missing_authorities"]) == set(MISSING)
    assert set(manifest["validated_observations"]) == set(LABELS)
    assert manifest["collection_manifest"] in manifest["artifacts"]
    assert "policy_revision" not in manifest
    assert result["observed_at"] is not None and result["age_seconds"] > 0
    assert api.finalize(lease) == result and observer.calls == 1
    restarted = service(authority)
    assert restarted.get(result["id"]) == result
    assert restarted.get_latest("pipeline-one") == result
    assert restarted.list_history("pipeline-one") == (result,)


def test_sql_outcome_cannot_promote_observations_to_admission(authority):
    api, lease, _ = observed(authority)
    result = api.finalize(lease)
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                           .values(outcome="verified", reason_code=None))
    read = service(authority).get(result["id"])
    assert read["outcome"] == "incomplete" and read["reason_code"] == "native_authority_unavailable"
    assert read["result_manifest"] == result["result_manifest"]


@pytest.mark.parametrize("field", ["primary_key", "columns"])
def test_rehashed_scope_must_still_match_frozen_sql_columns_and_key(authority, field):
    api, lease, _ = observed(authority)
    result = api.finalize(lease)
    s = scope()
    changed = replace(s, primary_key=("LABEL",)) if field == "primary_key" else replace(s, columns=s.columns[:2])
    manifest = deepcopy(result["result_manifest"])
    manifest["scope_sha256"] = changed.sha256
    report = manifest["comparison"]
    report["scope_sha256"] = changed.sha256
    raw = (canonical(report) + "\n").encode()
    comparison = reference("comparison", digest=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
    manifest["artifacts"] = [comparison.to_dict() if x["name"] == "comparison" else x for x in manifest["artifacts"]]
    encoded = canonical(manifest)
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
            .values(scope_json=canonical(changed.to_dict()), scope_sha256=changed.sha256,
                    result_manifest_json=encoded, result_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
    assert service(authority).get(result["id"])["reason_code"] == "stored_result_invalid"


@pytest.mark.parametrize("fault", ["report", "manifest", "context", "scope", "flags", "streams", "missing", "labels", "foreign"])
def test_observer_result_contract_is_validated_before_sql_persistence(authority, fault):
    def change(collection, replay):
        if fault == "report":
            report = replay.comparison
            report["datasets"]["source"]["row_count"] += 1
            replay = replace(replay, _comparison=report)
        elif fault == "manifest":
            replay = replace(replay, collection_manifest=reference("collection_manifest", digest="f" * 64))
        elif fault == "context":
            replay = replace(replay, context_sha256="f" * 64)
        elif fault == "scope":
            replay = replace(replay, scope_sha256="f" * 64)
        elif fault == "flags":
            replay = replace(replay, native_cdc_qualified=True)
        elif fault == "streams":
            replay = replace(replay, streams_replayed=True)
        elif fault == "missing":
            replay = replace(replay, missing_authorities=())
        elif fault == "labels":
            replay = replace(replay, validated_observations=("native_admitted",))
        else:
            foreign = reference("collection_manifest", org="org-other")
            collection = replace(collection, manifest=foreign,
                artifacts=tuple(foreign if x.name == "collection_manifest" else x for x in collection.artifacts))
            replay = replace(replay, collection_manifest=foreign)
        return collection, replay
    api, lease, _ = observed(authority, change=change)
    result = api.finalize(lease)
    assert result["outcome"] == "incomplete" and result["result_manifest"] is None


@pytest.mark.parametrize("label,removed", [("destination_identity", "destination:mirror:after"),
                                          ("receiver_sampled_ranges", "receiver:before"),
                                          ("native_source_observations", "native_bundle")])
def test_validated_labels_require_their_original_artifact_references(authority, label, removed):
    def change(collection, replay):
        assert label in replay.validated_observations
        return replace(collection, artifacts=tuple(x for x in collection.artifacts if x.name != removed)), replay
    api, lease, _ = observed(authority, change=change)
    assert api.finalize(lease)["result_manifest"] is None


@pytest.mark.parametrize("case", ["scope", "generation", "missing", "label", "stream", "manifest", "flags", "format"])
def test_rehashed_stored_observation_corruption_fails_closed(authority, case):
    api, lease, _ = observed(authority)
    result = api.finalize(lease)
    manifest = deepcopy(result["result_manifest"])
    if case == "scope":
        manifest["scope_sha256"] = "f" * 64
    elif case == "generation":
        manifest["context_generation"] = True
    elif case == "missing":
        manifest["missing_authorities"].remove("stable_cut")
    elif case == "label":
        manifest["validated_observations"].append("verified")
    elif case == "stream":
        next(x for x in manifest["artifacts"] if x["name"] == "stream:source")["sha256"] = "f" * 64
    elif case == "manifest":
        manifest["collection_manifest"]["sha256"] = "f" * 64
    elif case == "flags":
        manifest["native_cdc_qualified"] = True
    else:
        manifest["format_version"] = "quadringent-native-admitted-result-v1"
    encoded = canonical(manifest)
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                           .values(result_manifest_json=encoded, result_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
    read = service(authority).get(result["id"])
    assert read["outcome"] == "incomplete" and read["result_manifest"] is None
    assert read["reason_code"] == "stored_result_invalid"


@pytest.mark.parametrize("phase", ["generation", "expiry", "copy"])
def test_observation_callback_and_final_fence_reject_supersession(authority, phase):
    def during(context):
        if phase == "expiry":
            api.now = lambda: NOW + timedelta(hours=1)
        else:
            with authority[0].begin() as connection:
                semantic_update(connection, schema.sources if phase == "generation" else schema.pipelines,
                    "source-one" if phase == "generation" else "pipeline-one",
                    {"ibmi_host": "new.invalid"} if phase == "generation" else {"active_run_id": "other-copy"})
    api, lease, observer = observed(authority, during=during)
    with pytest.raises(NativeContextConflict):
        api.finalize(lease)
    assert observer.calls == 1
    assert api.get(lease.request_id)["result_manifest"] is None


def test_observer_and_admission_ports_are_distinct_and_admission_takes_precedence(authority):
    from test_v2_native_verifications import Controller
    api, lease, observer = observed(authority)
    api.controller = Controller(authority[1])
    assert api.finalize(lease)["outcome"] == "verified"
    assert observer.calls == 0


def test_service_checks_final_fence_even_if_observer_ignores_callback_after_io(authority):
    api, lease, observer = observed(authority)
    class Uncooperative:
        def observe(self, prepared, **kwargs):
            result = observer.observe(prepared, **kwargs)
            with authority[0].begin() as connection:
                semantic_update(connection, schema.sources, "source-one", {"ibmi_host": "changed.invalid"})
            return result
    api.observer = Uncooperative()
    with pytest.raises(NativeContextConflict):
        api.finalize(lease)
    assert api.get(lease.request_id)["result_manifest"] is None


def test_observation_without_destination_identity_keeps_that_authority_missing(authority):
    def change(collection, replay):
        return collection, replace(replay, validated_observations=(), missing_authorities=MISSING + ("destination_identity",))
    api, lease, _ = observed(authority, change=change)
    manifest = api.finalize(lease)["result_manifest"]
    assert manifest["validated_observations"] == []
    assert manifest["missing_authorities"] == list(MISSING) + ["destination_identity"]


def test_divergent_replay_is_still_an_incomplete_observation(authority):
    class Divergent(Observer):
        def observe(self, prepared, **kwargs):
            collection, replay = super().observe(prepared, **kwargs)
            values = layers(prepared.scope)
            values["mirror"][0] = {**values["mirror"][0], "LABEL": "different"}
            path = self.directory / "divergent-ledger"
            path.mkdir()
            report = run_all(path, prepared.scope, values)
            raw = (canonical(report) + "\n").encode()
            refs = tuple(reference(x.name, digest=report["datasets"]["mirror"]["sha256"],
                byte_count=report["datasets"]["mirror"]["byte_count"]) if x.name == "stream:mirror" else
                reference("comparison", digest=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
                if x.name == "comparison" else x for x in collection.artifacts)
            return replace(collection, _report=report, artifacts=refs), replace(replay, _comparison=report)
    api, lease, _ = observed(authority)
    api.observer = Divergent(authority[1])
    result = api.finalize(lease)
    assert result["outcome"] == "incomplete" and result["result_manifest"]["comparison"]["state"] == "divergent"


@pytest.fixture
def observed_api(native_api, authority):
    client, engine, _, factories, record = native_api
    observer = Observer(authority[1])
    client.app.state.native_verification_preparer_factory = lambda org: ObservationPreparer()
    client.app.state.native_verification_controller_factory = None
    client.app.state.native_verification_observer_factory = lambda org: observer
    return client, engine, observer, factories, record


def test_http_observation_readback_and_idempotence(observed_api):
    client, engine, observer, _, _ = observed_api
    capability = client.get(BASE + "/capabilities")
    assert capability.status_code == 200
    assert capability.json() == {"available": True, "mode": "observation", "reason_code": None}
    assert attempts(engine) == [] and observer.calls == 0
    response = post(client)
    assert response.status_code == 201, response.text
    result = response.json()["after"]
    assert result["outcome"] == "incomplete" and result["result_manifest"]["comparison"]["state"] == "verified"
    assert recorded_result(client.get(BASE + "/latest").json()) == recorded_result(result)
    assert recorded_result(client.get(BASE + "/" + result["id"]).json()) == recorded_result(result)
    assert [recorded_result(x) for x in client.get(BASE).json()["items"]] == [recorded_result(result)]
    assert post(client).json() == response.json() and observer.calls == 1
    with engine.begin() as connection:
        connection.execute(schema.idempotency_keys.delete())
    assert post(client).status_code == 201 and observer.calls == 1


@pytest.mark.parametrize("fault", ["unconfigured", "broken", "no_prepare", "disabled"])
def test_capabilities_are_fail_closed_without_invoking_collection(observed_api, fault):
    client, engine, observer, _, _ = observed_api
    def broken(org):
        raise RuntimeError("SYNTHETIC_PRIVATE_FACTORY")
    if fault == "unconfigured":
        client.app.state.native_verification_observer_factory = None
    elif fault == "broken":
        client.app.state.native_verification_observer_factory = broken
    elif fault == "no_prepare":
        client.app.state.native_verification_preparer_factory = lambda org: None
    else:
        with engine.begin() as connection:
            semantic_update(connection, schema.organizations, "org-one", {"native_context_enforced": False})
    response = client.get(BASE + "/capabilities")
    assert response.status_code == 200, response.text
    assert response.json() == {"available": False, "mode": None,
        "reason_code": "native_context_unavailable" if fault == "disabled" else "not_configured"}
    assert attempts(engine) == [] and observer.calls == 0
    assert post(client).status_code == 409


def test_capabilities_respect_authentication_source_and_organization_before_factories(observed_api):
    client, _, observer, _, _ = observed_api
    assert client.get(BASE.replace("pipeline-one", "pipeline-other") + "/capabilities").status_code == 404
    _, token = client.app.state.agent_tokens_service.create(name="Restricted", scope="read",
        source_restriction=["source-two"], never_expires=True, created_by="fixture")
    client.headers["Authorization"] = "Bearer " + token
    assert client.get(BASE + "/capabilities").status_code == 403
    client.headers.pop("Authorization")
    assert client.get(BASE + "/capabilities").status_code == 401 and observer.calls == 0


@pytest.mark.parametrize("body", [{"proofs": []}, {"observer": {}}, {"comparison": {}}, {"dry_run": "yes"}])
def test_observation_route_never_accepts_client_proofs(observed_api, body):
    client, engine, observer, _, _ = observed_api
    assert post(client, body=body).status_code == 400
    assert attempts(engine) == [] and observer.calls == 0


def test_cached_observation_cannot_hide_stored_manifest_corruption(observed_api):
    client, engine, observer, _, _ = observed_api
    result = post(client).json()["after"]
    with engine.begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                           .values(result_manifest_json='{"native_admission":"verified"}'))
    assert client.get(BASE + "/latest").json()["reason_code"] == "stored_result_invalid"
    assert post(client).status_code == 409 and observer.calls == 1


def test_terminal_without_manifest_cannot_be_promoted_by_sql_outcome(authority):
    api, lease, _ = observed(authority)
    api.observer = None
    result = api.finalize(lease)
    assert result["result_manifest"] is None
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with authority[0].begin() as connection:
            connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                               .values(outcome="verified"))
    # The SQL constraint protects writes; also reject an already-corrupt loaded row.
    with authority[0].connect() as connection:
        row = dict(connection.execute(select(schema.native_verifications).where(
            schema.native_verifications.c.id == result["id"])).mappings().one())
        row["outcome"] = "verified"
        assert service(authority)._view(connection, row)["outcome"] == "incomplete"


@pytest.mark.parametrize("generation", [True, 99])
def test_admitted_result_generation_revalidated_on_get(authority, generation):
    from test_v2_native_verifications import Controller
    api, lease, _ = observed(authority)
    api.controller = Controller(authority[1])
    result = api.finalize(lease)
    manifest = deepcopy(result["result_manifest"])
    manifest["context_generation"] = generation
    encoded = canonical(manifest)
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
            .values(result_manifest_json=encoded, result_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
    read = service(authority).get(result["id"])
    assert read["outcome"] == "incomplete" and read["result_manifest"] is None


@pytest.mark.parametrize("mutation", ["empty", "missing", "duplicate"])
def test_stored_preparation_requires_all_original_references(authority, mutation):
    api, lease, _ = observed(authority)
    result = api.finalize(lease)
    originals = [x for x in result["result_manifest"]["artifacts"] if x["name"] in
                 {"copy_intent", "copy_evidence", "registry", "binding", "checkpoint"}]
    originals = [] if mutation == "empty" else originals[:-1] if mutation == "missing" else originals + originals[:1]
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                           .values(preparation_json=canonical(originals)))
    assert service(authority).get(result["id"])["result_manifest"] is None


def test_preparation_manifest_extra_remains_compatible(authority):
    class WithManifest(ObservationPreparer):
        def prepare(self, context):
            prepared = super().prepare(context)
            return replace(prepared, artifacts=prepared.artifacts + (reference("preparation_manifest", preparation=True),))
    api = service(authority, preparer=WithManifest(), observer=Observer(authority[1]))
    attempt = api.request("pipeline-one", requested_by="fixture", idempotency_key="extra-manifest")
    lease = api.prepare(api.claim(attempt["id"], holder="observer"))
    assert api.finalize(lease)["result_manifest"] is not None


@pytest.mark.parametrize("admitted", [False, True])
def test_historical_result_keeps_its_frozen_columns_when_current_columns_change(authority, admitted):
    from test_v2_native_verifications import Controller
    api, lease, _ = observed(authority)
    if admitted:
        api.controller = Controller(authority[1])
    result = api.finalize(lease)
    with authority[0].begin() as connection:
        semantic_update(connection, schema.tables, "table-one", {
            "discovered_columns": [{"name": "CHANGED", "kind": "integer"}], "key_columns": "CHANGED"})
    read = service(authority).get(result["id"])
    assert read["context_validity"] == "superseded"
    assert read["result_manifest"] == result["result_manifest"]
    assert read["outcome"] == ("verified" if admitted else "incomplete")


def test_admitted_outcome_is_derived_from_the_valid_comparison(authority, monkeypatch):
    import test_v2_native_verifications as admitted_fixtures
    original = admitted_fixtures.layers
    def divergent_layers(s):
        values = original(s)
        values["mirror"][0] = {**values["mirror"][0], "LABEL": "different"}
        return values
    monkeypatch.setattr(admitted_fixtures, "layers", divergent_layers)
    api, lease, _ = observed(authority)
    api.controller = admitted_fixtures.Controller(authority[1])
    result = api.finalize(lease)
    assert result["outcome"] == "divergent"
    with authority[0].begin() as connection:
        connection.execute(schema.native_verifications.update().where(schema.native_verifications.c.id == result["id"])
                           .values(outcome="verified"))
    assert service(authority).get(result["id"])["outcome"] == "divergent"
