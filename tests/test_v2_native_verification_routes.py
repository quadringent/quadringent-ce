"""Native routes dispatch trusted ports and preserve the SQL authority fences."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.native_context import semantic_update
from test_v2_native_verifications import Controller, Preparer, authority  # noqa: F401

BASE = "/v2/pipelines/pipeline-one/native-verifications"


@pytest.fixture
def native_api(authority):
    engine, directory = authority
    controller, factories = Controller(directory), []

    def preparer_factory(org_id):
        factories.append(("prepare", org_id))
        return Preparer()

    def controller_factory(org_id):
        factories.append(("admit", org_id))
        return controller

    app = create_v2_app(
        engine=engine, secret_box=SecretBox(SecretBox.generate_key()), org_id="org-one",
        require_authentication=True, token_pepper=b"native-route-fixture",
        native_verification_preparer_factory=preparer_factory,
        native_verification_controller_factory=controller_factory,
    )
    client = TestClient(app)
    record, token = app.state.agent_tokens_service.create(
        name="Native operator", scope="operate", never_expires=True, created_by="fixture",
    )
    client.headers["Authorization"] = "Bearer " + token
    yield client, engine, controller, factories, record
    client.close()


def post(client, key="request-one", body=None):
    return client.post(BASE, json={} if body is None else body, headers={"Idempotency-Key": key})


def attempts(engine):
    with engine.connect() as connection:
        return connection.execute(select(schema.native_verifications)).mappings().all()


def recorded_result(result):
    return {key: value for key, value in result.items() if key != "age_seconds"}


def test_request_dispatches_real_ledger_and_exposes_bound_reads(native_api):
    client, engine, controller, factories, record = native_api
    response = post(client)
    assert response.status_code == 201, response.text
    body = response.json()
    result = body["after"]
    assert result["lifecycle"] == "finished" and result["outcome"] == "verified"
    assert result["context_validity"] == "same_context"
    assert result["result_manifest"]["comparison"]["transaction_consistency_verified"] is False
    assert controller.calls == 1
    assert factories == [("prepare", "org-one"), ("admit", "org-one")]
    assert recorded_result(client.get(BASE + "/latest").json()) == recorded_result(result)
    assert recorded_result(client.get(BASE + "/" + result["id"]).json()) == recorded_result(result)
    assert [recorded_result(item) for item in client.get(BASE).json()["items"]] == [recorded_result(result)]
    assert factories == [("prepare", "org-one"), ("admit", "org-one")]
    assert body["verify"] == {"method": "GET", "path": BASE + "/" + result["id"]}
    assert attempts(engine)[0]["requested_by"] == "agent:" + record.id
    with engine.connect() as connection:
        audit = connection.execute(select(schema.audit_records)).mappings().one()
    assert audit["actor_id"] == record.id and audit["resource_id"] == "pipeline-one"
    assert audit["action"] == "pipeline.native_verification.request"
    assert "first  " not in response.text


def test_http_and_service_idempotency_never_recollect_finished_request(native_api):
    client, engine, controller, factories, _ = native_api
    first = post(client)
    assert first.status_code == 201, first.text
    assert post(client).json() == first.json()
    assert len(factories) == 4 and controller.calls == 1
    with engine.begin() as connection:
        connection.execute(schema.idempotency_keys.delete())
    replay = post(client)
    assert replay.status_code == 201
    assert recorded_result(replay.json()["after"]) == recorded_result(first.json()["after"])
    assert len(factories) == 6 and controller.calls == 1 and len(attempts(engine)) == 1


def test_changed_idempotent_body_is_a_conflict(native_api):
    client, _, controller, _, _ = native_api
    assert post(client).status_code == 201
    response = post(client, body={"dry_run": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_conflict"
    assert controller.calls == 1


@pytest.mark.parametrize("disabled", ["enforcement", "controller"])
def test_cached_http_receipt_cannot_bypass_disabled_native_authority(native_api, disabled):
    client, engine, controller, _, _ = native_api
    assert post(client).status_code == 201
    if disabled == "enforcement":
        with engine.begin() as connection:
            semantic_update(connection, schema.organizations, "org-one", {"native_context_enforced": False})
    else:
        client.app.state.native_verification_controller_factory = None
    response = post(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capability_unavailable"
    assert controller.calls == 1 and len(attempts(engine)) == 1


@pytest.mark.parametrize("table,field,value", [
    (schema.sources, "ibmi_host", "changed.invalid"),
    (schema.tables, "key_strategy", "unique_index"),
])
def test_cached_receipt_is_refused_after_semantic_context_change_without_rewriting_it(
    native_api, table, field, value,
):
    client, engine, controller, _, _ = native_api
    first = post(client)
    assert first.status_code == 201
    with engine.connect() as connection:
        stored = connection.execute(select(schema.idempotency_keys.c.response)).scalar_one()
    with engine.begin() as connection:
        semantic_update(connection, table, "source-one" if table is schema.sources else "table-one", {field: value})
    assert client.get(BASE + "/latest").json()["context_validity"] == "superseded"
    replay = post(client)
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "capability_unavailable"
    assert controller.calls == 1 and len(attempts(engine)) == 1
    with engine.connect() as connection:
        assert connection.execute(select(schema.idempotency_keys.c.response)).scalar_one() == stored == first.json()


@pytest.mark.parametrize("missing", ["preparer", "controller"])
def test_cached_receipt_resolves_callable_factories_before_replay(native_api, missing):
    client, engine, controller, _, _ = native_api
    first = post(client)
    assert first.status_code == 201
    setattr(client.app.state, "native_verification_" + missing + "_factory", lambda org_id: None)
    replay = post(client)
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "capability_unavailable"
    assert controller.calls == 1 and len(attempts(engine)) == 1
    with engine.connect() as connection:
        assert connection.execute(select(schema.idempotency_keys.c.response)).scalar_one() == first.json()


@pytest.mark.parametrize("body", [{"artifacts": []}, {"comparison": {"state": "verified"}},
                                  {"scope": {}}, {"dry_run": "yes"}])
def test_caller_cannot_supply_proof_or_ambiguous_dry_run(native_api, body):
    client, engine, controller, _, _ = native_api
    response = post(client, body=body)
    assert response.status_code == 400, response.text
    assert attempts(engine) == [] and controller.calls == 0


def test_dry_run_has_no_request_or_collection_effect(native_api):
    client, engine, controller, _, _ = native_api
    response = post(client, body={"dry_run": True})
    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is not None
    assert attempts(engine) == [] and controller.calls == 0


@pytest.mark.parametrize("missing", ["preparer", "controller", "factory_result", "enforcement"])
def test_unavailable_dependencies_never_leave_a_pending_request(native_api, missing):
    client, engine, controller, _, _ = native_api
    if missing == "enforcement":
        with engine.begin() as connection:
            semantic_update(connection, schema.organizations, "org-one", {"native_context_enforced": False})
    elif missing == "factory_result":
        client.app.state.native_verification_controller_factory = lambda org_id: None
    else:
        setattr(client.app.state, "native_verification_" + missing + "_factory", None)
    response = post(client)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "capability_unavailable"
    assert attempts(engine) == [] and controller.calls == 0


@pytest.mark.parametrize("phase", ["prepare", "admit", "factory"])
def test_internal_errors_are_safe_and_collection_failures_finish_incomplete(native_api, phase):
    client, engine, _, _, _ = native_api

    class Broken:
        def prepare(self, context):
            raise RuntimeError("password=canary-route-secret")

        def admit(self, prepared):
            raise RuntimeError("password=canary-route-secret")

    if phase == "factory":
        def factory(org_id):
            raise RuntimeError("password=canary-route-secret")
        client.app.state.native_verification_controller_factory = factory
    else:
        setattr(client.app.state, "native_verification_" + ("preparer" if phase == "prepare" else "controller")
                + "_factory", lambda org_id: Broken())
    response = post(client)
    assert "canary-route-secret" not in response.text
    if phase == "factory":
        assert response.status_code == 409 and attempts(engine) == []
    else:
        assert response.status_code == 201, response.text
        assert response.json()["after"]["outcome"] == "incomplete"
        assert attempts(engine)[0]["lifecycle"] == "finished"


def test_superseded_context_is_visible_and_failure_never_falls_back_to_old_green(native_api):
    client, engine, _, _, _ = native_api
    first = post(client).json()["after"]
    with engine.begin() as connection:
        semantic_update(connection, schema.sources, "source-one", {"ibmi_host": "new.invalid"})
    assert client.get(BASE + "/latest").json()["context_validity"] == "superseded"
    assert client.get(BASE + "/" + first["id"]).json()["context_validity"] == "superseded"

    class Broken:
        def prepare(self, context):
            raise RuntimeError("unavailable")

    client.app.state.native_verification_preparer_factory = lambda org_id: Broken()
    second = post(client, key="new-context").json()["after"]
    assert second["outcome"] == "incomplete"
    assert client.get(BASE + "/latest").json()["id"] == second["id"]


def test_context_change_during_collection_closes_owned_attempt_without_publishing(native_api):
    client, engine, controller, _, _ = native_api

    class Superseding:
        def admit(self, prepared):
            admitted = controller.admit(prepared)
            with engine.begin() as connection:
                semantic_update(connection, schema.sources, "source-one", {"ibmi_host": "new.invalid"})
            return admitted

    client.app.state.native_verification_controller_factory = lambda org_id: Superseding()
    response = post(client)
    assert response.status_code == 201, response.text
    result = response.json()["after"]
    assert result["outcome"] == "incomplete" and result["context_validity"] == "superseded"
    assert result["result_manifest"] is None and attempts(engine)[0]["lifecycle"] == "finished"


def test_native_read_ids_must_belong_to_the_path_pipeline_and_organization(native_api):
    client, _, _, _, _ = native_api
    result = post(client).json()["after"]
    assert client.get(BASE + "/missing").status_code == 404
    assert client.get("/v2/pipelines/pipeline-two/native-verifications/" + result["id"]).status_code == 404
    foreign = "/v2/pipelines/pipeline-other/native-verifications"
    assert client.get(foreign).status_code == 404
    assert client.get(foreign + "/latest").status_code == 404
    assert client.get(foreign + "/" + result["id"]).status_code == 404
    assert client.post(foreign, json={}, headers={"Idempotency-Key": "foreign"}).status_code == 404


def test_authentication_roles_and_restricted_source_apply_before_ports(native_api):
    client, engine, controller, factories, _ = native_api
    client.headers.pop("Authorization")
    assert client.get(BASE).status_code == 401
    assert post(client).status_code == 401
    _, token = client.app.state.agent_tokens_service.create(
        name="Reader", scope="read", source_restriction=["source-one"], never_expires=True, created_by="fixture",
    )
    client.headers["Authorization"] = "Bearer " + token
    assert client.get(BASE).status_code == 200
    assert post(client).status_code == 403
    _, token = client.app.state.agent_tokens_service.create(
        name="Restricted", scope="operate", source_restriction=["source-two"],
        never_expires=True, created_by="fixture",
    )
    client.headers["Authorization"] = "Bearer " + token
    for path in (BASE, BASE + "/latest", BASE + "/missing"):
        assert client.get(path).status_code == 403
    assert post(client).status_code == 403
    assert attempts(engine) == [] and controller.calls == 0 and factories == []


@pytest.mark.parametrize("limit", [0, 501])
def test_history_limit_is_bounded(native_api, limit):
    assert native_api[0].get(BASE, params={"limit": limit}).status_code == 400
