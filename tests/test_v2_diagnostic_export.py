"""A private export must remain useful without secrets or invented live checks."""

from datetime import datetime, timedelta, timezone
import json
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from quadringent_control_plane.auth import AuthConfig
from quadringent_control_plane.v2 import db, schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox

CANARY = "PRIVATE_SECRET_CANARY"
ADMIN = {"X-User": "maintainer", "X-Groups": "administrators", "X-Quadringent-Proxy-Secret": "test-proxy"}


def seed(engine, org="org-private", suffix="", *, state="live", observed="running", observed_at=None):
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": org, "name": CANARY})
        connection.execute(schema.sources.insert(), {
            "id": "source"+suffix, "org_id": org, "display_name": CANARY,
            "ibmi_host": CANARY, "ibmi_user": CANARY, "secret_ciphertext": CANARY,
            "tls_pinned_pem": CANARY, "tls_fingerprint": CANARY,
        })
        connection.execute(schema.destinations.insert(), {
            "id": "destination"+suffix, "org_id": org, "snowflake_account": CANARY,
            "destination_database": CANARY, "destination_schema": CANARY,
            "key_pair_ciphertext": CANARY, "setup_script": CANARY,
        })
        connection.execute(schema.tables.insert(), {
            "id": "table"+suffix, "source_id": "source"+suffix,
            "schema_name": CANARY, "table_name": CANARY, "key_columns": CANARY,
            "cl_fix_commands": [CANARY], "discovered_columns": [{"name": CANARY}],
        })
        connection.execute(schema.pipelines.insert(), {
            "id": "pipeline"+suffix, "table_id": "table"+suffix, "destination_id": "destination"+suffix,
            "declared_state": state, "last_observed_state": observed,
            "last_observed_at": observed_at, "attention_reason": CANARY, "active_run_id": CANARY,
        })


class NoExternalCalls:
    def __getattr__(self, _name):
        raise AssertionError("An export must not ask a provider for live observations")


def app_for(engine, *, proxy=True):
    return create_v2_app(
        engine=engine, org_id="org-private", secret_box=SecretBox(SecretBox.generate_key()),
        auth_config=AuthConfig.build(user_header="X-User", groups_header="X-Groups",
                                     admin_groups="administrators", operator_groups="operators", proxy_secret="test-proxy") if proxy else None,
        token_pepper=b"test-diagnostic-token-pepper",
        pipeline_observation_provider=NoExternalCalls(), log_source=NoExternalCalls(),
        source_probe=NoExternalCalls(), costs_provider=NoExternalCalls(),
    )


@pytest.fixture
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'diagnostics.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    seed(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("path", ["/v2/diagnostics", "/v2/diagnostics/export"])
def test_requires_an_authenticated_unrestricted_admin(engine, path):
    client = TestClient(app_for(engine))
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-User": "forged", "X-Groups": "administrators"}).status_code == 401
    for group in ["operators", "readers"]:
        assert client.get(path, headers={**ADMIN, "X-Groups": group}).status_code == 403
    assert TestClient(app_for(engine, proxy=False)).get(path).status_code == 401
    app = app_for(engine)
    _, token = app.state.agent_tokens_service.create(name="restricted", scope="admin", source_restriction=["source"], created_by="test", never_expires=True)
    assert TestClient(app).get(path, headers={"Authorization": "Bearer "+token}).status_code == 403


def test_export_download_is_private_bounded_and_has_no_live_claim(engine):
    client = TestClient(app_for(engine))
    capability = client.get("/v2/diagnostics", headers=ADMIN)
    assert capability.status_code == 200
    assert capability.json()["export"]["state"] == "available"
    response = client.get("/v2/diagnostics/export", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'attachment; filename="quadringent-diagnostic.json"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.content) < 1_048_576
    payload = response.json()
    assert payload["format_version"] == "quadringent-diagnostic-v1"
    assert payload["collection"]["mode"] == "local_database_snapshot"
    assert payload["collection"]["live_checks_performed"] is False
    assert payload["counts"] == {"sources": 1, "destinations": 1, "tables": 1, "pipelines": 1}
    assert payload["pipeline_inventory"]["complete"] is True
    item = payload["pipeline_inventory"]["items"][0]
    assert item["declared_state"] == "live"
    assert item["stored_observation"] == {"state": "running", "last_state_change_recorded_at": None, "quality": "timestamp_missing", "provenance": "stored_control_plane_state", "measurement_provenance": "not_stored"}
    assert item["delivery_evidence"] == "not_collected"
    for forbidden in [CANARY, "org-private"]:
        assert forbidden not in response.text
    for value in ["source", "destination", "table", "pipeline"]:
        assert '"'+value+'"' not in response.text


def test_export_filters_both_source_and_destination_organization(engine):
    seed(engine, org="other-private", suffix="-foreign")
    with engine.begin() as connection:
        connection.execute(schema.tables.insert(), {"id": "cross-table", "source_id": "source", "schema_name": "X", "table_name": "CROSS"})
        connection.execute(schema.pipelines.insert(), {"id": "cross", "table_id": "cross-table", "destination_id": "destination-foreign", "declared_state": "live"})
    response = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["counts"] == {"sources": 1, "destinations": 1, "tables": 2, "pipelines": 1}
    assert len(response.json()["pipeline_inventory"]["items"]) == 1
    assert "foreign" not in response.text and "other-private" not in response.text


def test_inventory_truncation_is_explicit_and_does_not_drop_total_count(engine, monkeypatch):
    from quadringent_control_plane.v2.services import diagnostics
    monkeypatch.setattr(diagnostics, "MAX_PIPELINES", 1)
    with engine.begin() as connection:
        connection.execute(schema.tables.insert(), {"id": "table2", "source_id": "source", "schema_name": "X", "table_name": "TWO"})
        connection.execute(schema.pipelines.insert(), {"id": "pipeline2", "table_id": "table2", "destination_id": "destination", "declared_state": "paused"})
    result = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN).json()
    assert result["counts"]["pipelines"] == 2
    assert result["pipeline_inventory"]["complete"] is False
    assert len(result["pipeline_inventory"]["items"]) == 1


def test_resource_aliases_hide_free_ids_and_cannot_be_linked_between_exports(engine):
    with engine.begin() as connection:
        connection.execute(schema.sources.insert(), {"id": CANARY, "org_id": "org-private", "display_name": CANARY, "ibmi_host": CANARY, "ibmi_user": CANARY, "secret_ciphertext": CANARY})
        connection.execute(schema.tables.insert(), {"id": CANARY, "source_id": CANARY, "schema_name": "X", "table_name": "ONE"})
        connection.execute(schema.tables.insert(), {"id": CANARY+"2", "source_id": CANARY, "schema_name": "X", "table_name": "TWO"})
        connection.execute(schema.pipelines.insert(), {"id": CANARY, "table_id": CANARY, "destination_id": "destination", "declared_state": "paused"})
        connection.execute(schema.pipelines.insert(), {"id": CANARY+"2", "table_id": CANARY+"2", "destination_id": "destination", "declared_state": "paused"})
    client = TestClient(app_for(engine))
    responses = [client.get("/v2/diagnostics/export", headers=ADMIN) for _ in range(2)]
    for response in responses:
        assert response.status_code == 200 and CANARY not in response.text
    items = responses[0].json()["pipeline_inventory"]["items"]
    assert items[0]["source_ref"] == items[1]["source_ref"]
    assert items[0]["pipeline_ref"] != items[1]["pipeline_ref"]
    assert items[0]["source_ref"] != responses[1].json()["pipeline_inventory"]["items"][0]["source_ref"]


@pytest.mark.parametrize("delta,quality", [(None, "timestamp_missing"), (-3600, "unattributed_cached"), (3600, "future_clock_untrusted")])
def test_old_missing_or_future_timestamps_do_not_become_fresh_evidence(engine, delta, quality):
    at = None if delta is None else datetime.now(timezone.utc)+timedelta(seconds=delta)
    with engine.begin() as connection:
        connection.execute(schema.pipelines.update().values(last_observed_at=at))
    result = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN).json()
    assert result["pipeline_inventory"]["items"][0]["stored_observation"]["quality"] == quality
    assert "live_checks_performed" in json.dumps(result) and result["collection"]["live_checks_performed"] is False


def test_invalid_free_text_states_are_omitted_not_redacted_by_regex(engine):
    with engine.begin() as connection:
        connection.execute(schema.pipelines.update().values(declared_state=CANARY, last_observed_state=CANARY))
    response = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN)
    assert response.status_code == 200 and CANARY not in response.text
    item = response.json()["pipeline_inventory"]["items"][0]
    assert item["declared_state"] is None
    assert item["stored_observation"]["state"] is None


@pytest.mark.parametrize("state", ["healthy", "degraded", "incident", "unknown", "planned_stop", "awaiting_resume"])
def test_observed_health_states_are_preserved_independently_of_declared_state(engine, state):
    with engine.begin() as connection:
        connection.execute(schema.pipelines.update().values(last_observed_state=state))
    item = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN).json()["pipeline_inventory"]["items"][0]
    assert item["declared_state"] == "live"
    assert item["stored_observation"]["state"] == state
    assert item["stored_observation"]["measurement_provenance"] == "not_stored"


def test_database_failure_has_a_safe_action_and_never_copies_error_details(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from quadringent_control_plane.v2.services.diagnostics import DiagnosticsService
    def fail(_self):
        raise OperationalError(CANARY, {}, Exception(CANARY))
    monkeypatch.setattr(DiagnosticsService, "snapshot", fail)
    response = TestClient(app_for(engine)).get("/v2/diagnostics/export", headers=ADMIN)
    assert response.status_code == 503 and CANARY not in response.text
    assert response.json()["error"]["next_action"]


@pytest.mark.postgres
def test_postgresql_export_reads_a_real_snapshot(postgres_dsn):
    from quadringent_control_plane.v2.services.diagnostics import DiagnosticsService
    engine = db.create_engine_for(postgres_dsn)
    db.run_migrations(postgres_dsn)
    suffix = "-"+uuid.uuid4().hex[:12]
    org = "diag"+suffix
    seed(engine, org=org, suffix=suffix)
    writer = db.create_engine_for(postgres_dsn)
    inserted = []
    def concurrent_commit(_connection, _cursor, statement, _parameters, _context, _many):
        if inserted or "FROM sources" not in statement or "count(" not in statement:
            return
        inserted.append(True)
        with writer.begin() as connection:
            connection.execute(schema.sources.insert(), {"id": "source"+suffix+"-new", "org_id": org, "display_name": CANARY, "ibmi_host": CANARY, "ibmi_user": CANARY, "secret_ciphertext": CANARY})
            connection.execute(schema.destinations.insert(), {"id": "destination"+suffix+"-new", "org_id": org, "snowflake_account": CANARY, "key_pair_ciphertext": CANARY, "setup_script": CANARY})
            connection.execute(schema.tables.insert(), {"id": "table"+suffix+"-new", "source_id": "source"+suffix+"-new", "schema_name": "X", "table_name": "NEW"})
            connection.execute(schema.pipelines.insert(), {"id": "pipeline"+suffix+"-new", "table_id": "table"+suffix+"-new", "destination_id": "destination"+suffix+"-new", "declared_state": "paused"})
    event.listen(engine, "after_cursor_execute", concurrent_commit)
    try:
        result = DiagnosticsService(engine, org_id=org).snapshot()
        assert inserted == [True]
        assert result["counts"] == {"sources": 1, "destinations": 1, "tables": 1, "pipelines": 1}
        assert len(result["pipeline_inventory"]["items"]) == 1
        assert result["database_backend"] == "postgresql"
        assert result["collection"]["mode"] == "local_database_snapshot"
        assert DiagnosticsService(engine, org_id=org).snapshot()["counts"] == {"sources": 2, "destinations": 2, "tables": 2, "pipelines": 2}
    finally:
        event.remove(engine, "after_cursor_execute", concurrent_commit)
        with writer.begin() as connection:
            connection.execute(schema.pipelines.delete().where(schema.pipelines.c.id.in_(["pipeline"+suffix, "pipeline"+suffix+"-new"])))
            connection.execute(schema.tables.delete().where(schema.tables.c.source_id.in_(["source"+suffix, "source"+suffix+"-new"])))
            connection.execute(schema.destinations.delete().where(schema.destinations.c.org_id == org))
            connection.execute(schema.sources.delete().where(schema.sources.c.org_id == org))
            connection.execute(schema.organizations.delete().where(schema.organizations.c.id == org))
        writer.dispose()
        engine.dispose()
