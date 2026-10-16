"""Real bearer tokens must remain within their declared source set."""

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db, schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.auth import Identity
from quadringent_control_plane.v2.services.confirmations import ConfirmationsService
from quadringent_control_plane.v2.services.costs import NullCostsProvider
from quadringent_control_plane.v2.services.evidence import absent_evidence
from quadringent_control_plane.v2.services.observation import NullObservationProvider


class RecordingProvider(NullObservationProvider):
    def __init__(self):
        self.calls = []

    def observe(self, pipeline_id):
        self.calls.append(pipeline_id)
        return super().observe(pipeline_id)

    def evidence(self, pipeline_id):
        self.calls.append(pipeline_id)
        return absent_evidence(pipeline_id, reason="Not measured.")

    def metrics(self, pipeline_id, window):
        self.calls.append(pipeline_id)
        return super().metrics(pipeline_id, window)


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, **values):
        self.calls.append(values)


class RecordingLogs:
    def __init__(self):
        self.calls = []

    def fetch(self, pipeline_id, *, since):
        self.calls.append(pipeline_id)
        return ()


@pytest.fixture
def scoped(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'scope.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    with engine.begin() as connection:
        for org in ("default", "other"):
            connection.execute(schema.organizations.insert(), {"id": org, "name": "Fixture"})
        connection.execute(schema.destinations.insert(), {
            "id": "destination", "org_id": "default", "snowflake_account": "fixture",
            "key_pair_ciphertext": "x", "setup_script": "--",
        })
        for suffix in ("a", "b", "c", "foreign"):
            connection.execute(schema.sources.insert(), {
                "id": f"source-{suffix}", "org_id": "other" if suffix == "foreign" else "default",
                "display_name": "Fixture", "ibmi_host": "fixture.invalid", "ibmi_user": "U",
                "secret_ciphertext": "x",
            })
            connection.execute(schema.tables.insert(), {
                "id": f"table-{suffix}", "source_id": f"source-{suffix}",
                "schema_name": "L", "table_name": "T", "readiness": "ready",
            })
            connection.execute(schema.pipelines.insert(), {
                "id": f"pipeline-{suffix}", "table_id": f"table-{suffix}",
                "destination_id": "destination", "declared_state": "live",
            })
    executor, provider, logs = RecordingExecutor(), RecordingProvider(), RecordingLogs()
    app = create_v2_app(
        engine=engine, secret_box=SecretBox(SecretBox.generate_key()), org_id="default",
        token_pepper=b"synthetic-test-pepper", require_authentication=True, pipeline_executor=executor,
        pipeline_observation_provider=provider, log_source=logs,
    )
    app.state.sse_max_iterations = 1
    record, token = app.state.agent_tokens_service.create(
        name="Scoped agent", scope="admin", source_restriction=["source-a", "source-c"],
        never_expires=True, created_by="fixture", pre_authorized_actions=["pipeline.remove"],
    )
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    yield client, engine, executor, provider, logs, record
    engine.dispose()


def denied(response):
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "wrong_environment"


@pytest.mark.parametrize("path", [
    "/v2/sources/source-b", "/v2/sources/source-b/tables", "/v2/pipelines/pipeline-b",
    "/v2/pipelines/pipeline-b/evidence", "/v2/pipelines/pipeline-b/metrics",
    "/v2/pipelines/pipeline-b/logs",
])
def test_direct_and_indirect_reads_refuse_before_provider(scoped, path):
    client, _, executor, provider, logs, _ = scoped
    denied(client.get(path))
    assert not executor.calls and not provider.calls and not logs.calls


@pytest.mark.parametrize("method,path,body", [
    ("post", "/v2/sources/source-b/test", {}),
    ("post", "/v2/sources/source-b/actions/pause", {}),
    ("post", "/v2/sources/source-b/tables/refresh", {"dry_run": True}),
    ("post", "/v2/tables/table-b/pipeline", {"dry_run": True}),
    ("patch", "/v2/tables/table-b", {"dry_run": True}),
    ("put", "/v2/tables/table-b/discovered-columns", {"columns": []}),
    ("post", "/v2/pipelines/pipeline-b/actions/pause", {}),
    ("post", "/v2/pipelines/pipeline-b/actions/remove", {}),
])
def test_outside_mutations_and_dry_runs_are_refused(scoped, method, path, body):
    client, _, executor, provider, logs, _ = scoped
    denied(client.request(method, path, json=body, headers={"Idempotency-Key": "outside"}))
    assert not executor.calls and not provider.calls and not logs.calls


@pytest.mark.parametrize("path", [
    "/v2/sources/source-a", "/v2/sources/source-a/tables", "/v2/pipelines/pipeline-a",
    "/v2/pipelines/pipeline-a/evidence", "/v2/pipelines/pipeline-a/metrics",
    "/v2/pipelines/pipeline-a/logs", "/v2/auth/me",
])
def test_allowed_reads_remain_available(scoped, path):
    assert scoped[0].get(path).status_code == 200


def test_allowed_action_and_sources_list(scoped):
    client, _, executor, _, _, _ = scoped
    response = client.post("/v2/pipelines/pipeline-a/actions/pause", json={},
                           headers={"Idempotency-Key": "allowed"})
    assert response.status_code == 200
    assert executor.calls == [{"pipeline_id": "pipeline-a", "event": "pause"}]
    assert {r["id"] for r in client.get("/v2/sources").json()["items"]} == {"source-a", "source-c"}


def test_pipeline_pagination_filters_before_provider_and_page_limit(scoped):
    client, _, _, provider, _, _ = scoped
    first = client.get("/v2/pipelines?limit=1").json()
    assert [r["id"] for r in first["items"]] == ["pipeline-a"]
    assert first["next_cursor"] == "pipeline-a"
    second = client.get("/v2/pipelines", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [r["id"] for r in second["items"]] == ["pipeline-c"]
    assert second["next_cursor"] is None
    assert provider.calls == ["pipeline-a", "pipeline-c"]
    denied(client.get("/v2/pipelines?source_id=source-b"))


@pytest.mark.parametrize("path", [
    "/v2/sources/source-foreign", "/v2/sources/source-foreign/tables",
    "/v2/pipelines/pipeline-foreign", "/v2/pipelines/missing",
])
def test_foreign_or_absent_targets_still_return_not_found(scoped, path):
    assert scoped[0].get(path).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("post", "/v2/actions/pause_all"), ("post", "/v2/actions/resume_all"),
    ("get", "/v2/events"), ("get", "/v2/diagnostics"), ("get", "/v2/diagnostics/report"),
    ("get", "/v2/diagnostics/report/export"), ("get", "/v2/diagnostics/export"),
    ("get", "/v2/destinations"), ("get", "/v2/destinations/destination"),
    ("get", "/v2/destinations/destination/setup-script"),
    ("post", "/v2/destinations/destination/verify"),
    ("post", "/v2/destinations/destination/actions/pause"),
    ("post", "/v2/destinations"), ("post", "/v2/sources"),
    ("get", "/v2/agent-tokens"), ("post", "/v2/agent-tokens"),
    ("post", "/v2/setup/first-admin"), ("post", "/v2/users/activate"),
    ("post", "/v2/users/unknown/activate"),
    ("get", "/v2/users"), ("post", "/v2/users"), ("get", "/v2/audit"),
    ("get", "/v2/webhooks"), ("post", "/v2/webhooks"), ("get", "/v2/confirmations"),
    ("get", "/v2/costs?scope=connection&id=destination"),
])
def test_restricted_admin_cannot_escalate_through_global_routes(scoped, method, path):
    client, _, executor, provider, logs, _ = scoped
    denied(client.request(method, path, json={} if method == "post" else None,
                          headers={"Idempotency-Key": "global"}))
    assert not executor.calls and not provider.calls and not logs.calls


@pytest.mark.parametrize("scope,target,status", [
    ("table", "table-a", 200), ("table", "table-b", 403),
    ("table", "table-foreign", 404),
])
def test_table_costs_are_resolved_before_provider(scoped, scope, target, status):
    client = scoped[0]
    class Costs(NullCostsProvider):
        def __init__(self):
            self.calls = []

        def get(self, scope, resource_id, *, window=None):
            self.calls.append((scope, resource_id))
            return super().get(scope, resource_id, window=window)

    costs = Costs()
    client.app.state.costs_provider = costs
    assert client.get("/v2/costs", params={"scope": scope, "id": target}).status_code == status
    assert costs.calls == ([(scope, target)] if status == 200 else [])


def confirmation(scoped, suffix):
    client, engine, *_ = scoped
    service = ConfirmationsService(engine, org_id="default", pepper=client.app.state.token_pepper)
    return service.create(action_ref="pipeline.remove", resource_type="pipeline",
                          resource_id=f"pipeline-{suffix}", reason="Fixture", requested_by_kind="human",
                          requested_by_id="fixture")


@pytest.mark.parametrize("operation", ["", "/reject", "/approve"])
def test_confirmations_resolve_the_original_target(scoped, operation):
    client = scoped[0]
    allowed, _ = confirmation(scoped, "a")
    outside, _ = confirmation(scoped, "b")
    if operation:
        denied(client.post(f"/v2/confirmations/{outside.id}{operation}", json={},
                           headers={"Idempotency-Key": "outside-confirmation"}))
        assert client.post(f"/v2/confirmations/{allowed.id}{operation}", json={},
                           headers={"Idempotency-Key": "allowed-confirmation"}).status_code == 200
    else:
        denied(client.get(f"/v2/confirmations/{outside.id}"))
        assert client.get(f"/v2/confirmations/{allowed.id}").status_code == 200


def test_signed_link_cannot_bypass_presented_token_restriction(scoped):
    client = scoped[0]
    outside, link = confirmation(scoped, "b")
    denied(client.post(f"/v2/confirmations/{outside.id}/approve", json={"token": link},
                       headers={"Idempotency-Key": "signed-outside"}))
    client.headers.pop("Authorization")
    assert client.post(f"/v2/confirmations/{outside.id}/approve", json={"token": link},
                       headers={"Idempotency-Key": "signed-anonymous"}).status_code == 200


def test_cached_response_is_reauthorized_after_restriction_changes(scoped):
    client, engine, executor, _, _, record = scoped
    path = "/v2/pipelines/pipeline-a/actions/pause"
    key = {"Idempotency-Key": "cached-scope"}
    assert client.post(path, json={}, headers=key).status_code == 200
    with engine.begin() as connection:
        connection.execute(schema.agent_tokens.update().where(schema.agent_tokens.c.id == record.id)
                           .values(source_restriction=["source-c"]))
    denied(client.post(path, json={}, headers=key))
    assert len(executor.calls) == 1


def test_signed_confirmation_replay_rechecks_current_scope(scoped):
    client, engine, _, _, _, record = scoped
    allowed, link = confirmation(scoped, "a")
    path = f"/v2/confirmations/{allowed.id}/approve"
    key = {"Idempotency-Key": "cached-link-scope"}
    assert client.post(path, json={"token": link}, headers=key).status_code == 200
    with engine.begin() as connection:
        connection.execute(schema.agent_tokens.update().where(schema.agent_tokens.c.id == record.id)
                           .values(source_restriction=["source-c"]))
    denied(client.post(path, json={"token": link}, headers=key))


def test_confirmation_without_a_source_bound_target_is_refused(scoped):
    client, engine, *_ = scoped
    service = ConfirmationsService(engine, org_id="default", pepper=client.app.state.token_pepper)
    record, link = service.create(action_ref="organization.pause_all", resource_type="organization",
                                  resource_id="default", reason="Fixture", requested_by_kind="human",
                                  requested_by_id="fixture")
    denied(client.get(f"/v2/confirmations/{record.id}"))
    denied(client.post(f"/v2/confirmations/{record.id}/approve", json={"token": link},
                       headers={"Idempotency-Key": "unscoped-confirmation"}))


def test_confirmation_from_another_organization_is_not_found(scoped):
    client, engine, *_ = scoped
    service = ConfirmationsService(engine, org_id="other", pepper=client.app.state.token_pepper)
    record, _ = service.create(action_ref="pipeline.remove", resource_type="pipeline",
                                resource_id="pipeline-foreign", reason="Fixture",
                                requested_by_kind="human", requested_by_id="fixture")
    assert client.get(f"/v2/confirmations/{record.id}").status_code == 404


def test_unrestricted_admin_keeps_global_and_cross_source_access(scoped):
    client = scoped[0]
    _, token = client.app.state.agent_tokens_service.create(
        name="Unrestricted", scope="admin", never_expires=True, created_by="fixture",
    )
    client.headers["Authorization"] = "Bearer " + token
    assert client.get("/v2/sources/source-b").status_code == 200
    assert {r["id"] for r in client.get("/v2/pipelines").json()["items"]} == {
        "pipeline-a", "pipeline-b", "pipeline-c",
    }
    assert client.get("/v2/destinations").status_code == 200
    assert client.get("/v2/agent-tokens").status_code == 200


def test_missing_source_is_not_in_a_restricted_identity_scope():
    identity = Identity("fixture", "read", actor_kind="agent", source_restriction=("source-a",))
    assert not identity.source_allowed(None)
    assert identity.source_allowed("source-a")


def test_route_scope_is_preserved_with_a_reverse_proxy_root_path(scoped):
    client = TestClient(scoped[0].app, root_path="/proxy")
    client.headers.update(scoped[0].headers)
    denied(client.get("/v2/pipelines/pipeline-b/evidence"))

