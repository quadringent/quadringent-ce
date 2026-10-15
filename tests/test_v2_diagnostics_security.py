"""Diagnostic reads preserve source restrictions, organization boundaries and redaction."""

import json
import uuid

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.evidence import EvidenceStage, PipelineEvidence
from test_v2_diagnostics import ForbiddenExecutor, Provider, engine as engine, seed


@pytest.fixture
def world(engine):
    seed(engine, "default", prefix="a")
    seed(engine, "other", prefix="b")
    with engine.begin() as connection:
        connection.execute(schema.sources.insert(), {
            "id": "csrc", "org_id": "default", "display_name": "Second synthetic source",
            "ibmi_host": "offline.invalid", "ibmi_user": "OFFLINE", "secret_ciphertext": "unused"})
        connection.execute(schema.tables.insert(), {
            "id": "ctbl", "source_id": "csrc", "schema_name": "TESTLIB", "table_name": "SECOND"})
        connection.execute(schema.pipelines.insert(), {
            "id": "cppl", "table_id": "ctbl", "destination_id": "adst", "declared_state": "live"})
        connection.execute(schema.tables.insert(), {
            "id": "cross-table", "source_id": "asrc", "schema_name": "TESTLIB", "table_name": "CROSS"})
        connection.execute(schema.pipelines.insert(), {
            "id": "cross-pipeline", "table_id": "cross-table", "destination_id": "bdst", "declared_state": "live"})
    return engine


def authenticated_client(engine, provider, *, scope="read", restriction=()):
    app = create_v2_app(
        engine=engine, secret_box=SecretBox(SecretBox.generate_key()), org_id="default",
        token_pepper=b"offline-diagnostics-security", require_authentication=True,
        pipeline_observation_provider=provider, pipeline_executor=ForbiddenExecutor())
    _, token = app.state.agent_tokens_service.create(
        name="Offline security test", scope=scope, source_restriction=restriction,
        created_by="offline test", never_expires=True)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    return client


def report_document(response):
    document = response.json()
    return json.loads(document["files"]["report.json"]) if "files" in document else document


def pipeline_refs(document):
    return {item["subject"]["ref"] for item in document["results"]
            if item.get("subject") and item["subject"]["kind"] == "pipeline"}


@pytest.mark.parametrize("scope", ["read", "operate", "admin"])
@pytest.mark.parametrize("path", [
    "/v2/diagnostics/report", "/v2/diagnostics/report?pipeline_id=cppl", "/v2/diagnostics/report/export",
])
def test_source_restricted_tokens_cannot_read_organization_diagnostics(world, scope, path):
    provider = Provider()
    response = authenticated_client(world, provider, scope=scope, restriction=["asrc"]).get(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "wrong_environment"
    assert provider.calls == []


@pytest.mark.parametrize("scope", ["read", "operate", "admin"])
@pytest.mark.parametrize("path", ["/v2/diagnostics/report", "/v2/diagnostics/report/export"])
def test_unrestricted_read_scope_remains_sufficient(world, scope, path):
    response = authenticated_client(world, Provider(), scope=scope).get(path)
    assert response.status_code == 200
    assert report_document(response)["scope"] == "control_plane"


@pytest.mark.parametrize("path", ["/v2/diagnostics/report", "/v2/diagnostics/report/export"])
def test_foreign_destination_is_excluded_before_evidence_read(world, path):
    provider = Provider()
    response = authenticated_client(world, provider).get(path)
    assert response.status_code == 200
    document = report_document(response)
    assert len(pipeline_refs(document)) == 2
    assert set(provider.calls) == {"appl", "cppl"}
    assert "cross-pipeline" not in response.text and "bppl" not in response.text


@pytest.mark.parametrize("pipeline_id", ["cross-pipeline", "bppl"])
def test_filtered_foreign_pipeline_is_not_found_without_evidence_read(world, pipeline_id):
    provider = Provider()
    response = authenticated_client(world, provider).get("/v2/diagnostics/report", params={"pipeline_id": pipeline_id})
    assert response.status_code == 404
    assert provider.calls == []


@pytest.mark.parametrize("field", ["attention_reason", "reading", "delivery", "reconciliation"])
@pytest.mark.parametrize("payload_kind", ["credential", "row"])
def test_report_and_export_redact_free_text_before_truncation(world, field, payload_kind):
    marker = "offline-" + uuid.uuid4().hex
    prefix = "x" * (269 if field == "attention_reason" else 164) + " "
    detail = prefix + ("password=" + marker if payload_kind == "credential" else
                       json.dumps({"column": marker, "value": 7}))
    stages = {}
    if field == "attention_reason":
        with world.begin() as connection:
            connection.execute(schema.pipelines.update().where(schema.pipelines.c.id == "appl")
                               .values(declared_state="attention", attention_reason=detail))
    else:
        stages[field] = EvidenceStage(state="observed", observed_at="2026-10-15T18:00:00Z", detail=detail)
    provider = Provider({"appl": PipelineEvidence("appl", provenance="kubernetes_loader_log",
                                                 evidence_kind="live", **stages)})
    client = authenticated_client(world, provider)
    response = client.get("/v2/diagnostics/report", params={"pipeline_id": "appl"})
    assert response.status_code == 200
    assert marker[:16] not in response.text
    document = response.json()
    if field == "attention_reason":
        masked = next(item["evidence"]["attention_reason"] for item in document["results"] if item["code"] == "pipeline.state")
        assert len(masked) <= 300
    else:
        item = next(item for item in document["results"] if item["code"] == {
            "reading": "capture.reading", "delivery": "delivery.snowflake", "reconciliation": "reconciliation.destination"}[field])
        assert item["status"] == "passed" and len(item["limits"]) <= 225
        masked = item["limits"]
    assert ("password=[SECRET_MASQUE]" if payload_kind == "credential" else '{"redacted": "') in masked
    exported = client.get("/v2/diagnostics/report/export")
    assert exported.status_code == 200 and marker[:16] not in exported.text


@pytest.mark.parametrize("path", ["/v2/diagnostics/report", "/v2/diagnostics/report/export"])
def test_diagnostic_payloads_are_not_cacheable(world, path):
    response = authenticated_client(world, Provider()).get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("field", ["attention_reason", "reading", "delivery", "reconciliation"])
@pytest.mark.parametrize("payload_kind", ["quoted_json_password", "authorization_bearer"])
@pytest.mark.parametrize("path", ["/v2/diagnostics/report", "/v2/diagnostics/report/export"])
@pytest.mark.parametrize("near_limit", [False, True])
def test_quoted_credentials_and_bearer_values_are_redacted_before_truncation(world, field, payload_kind, path, near_limit):
    marker = "SYNTHETIC_DIAGNOSTIC_CANARY"
    limit = 300 if field == "attention_reason" else 200
    prefix_size = limit - (30 if payload_kind == "quoted_json_password" else 45)
    prefix = ("x" * (prefix_size - 1) + " ") if near_limit else ""
    detail = prefix + (json.dumps({"password": marker}) if payload_kind == "quoted_json_password" else
                       "Authorization: Bearer " + marker)
    stages = {}
    if field == "attention_reason":
        with world.begin() as connection:
            connection.execute(schema.pipelines.update().where(schema.pipelines.c.id == "appl")
                               .values(declared_state="attention", attention_reason=detail))
    else:
        stages[field] = EvidenceStage(state="observed", observed_at="2026-10-15T18:00:00Z", detail=detail)
    provider = Provider({"appl": PipelineEvidence("appl", provenance="kubernetes_loader_log",
                                                 evidence_kind="live", **stages)})
    response = authenticated_client(world, provider).get(path)
    assert response.status_code == 200
    assert marker[:8] not in response.text


@pytest.mark.parametrize("field", ["attention_reason", "reading", "delivery", "reconciliation"])
@pytest.mark.parametrize("path", ["/v2/diagnostics/report", "/v2/diagnostics/report/export"])
@pytest.mark.parametrize("scheme", ["Bearer", "Basic", "Negotiate", "NTLM", "Digest"])
@pytest.mark.parametrize("quoted_json", [False, True])
def test_complete_authorization_values_never_leave_diagnostics(world, field, path, scheme, quoted_json):
    value = (scheme + ' username="SYNTHETIC_ROOT_USER", realm="SYNTHETIC_ROOT_REALM", response="SYNTHETIC_ROOT_CANARY"'
             if scheme == "Digest" else scheme + " SYNTHETIC_ROOT_CANARY")
    detail = json.dumps({"Authorization": value}) if quoted_json else "Authorization: " + value
    stages = {}
    if field == "attention_reason":
        with world.begin() as connection:
            connection.execute(schema.pipelines.update().where(schema.pipelines.c.id == "appl")
                               .values(declared_state="attention", attention_reason=detail))
    else:
        stages[field] = EvidenceStage(state="observed", observed_at="2026-10-15T18:00:00Z", detail=detail)
    provider = Provider({"appl": PipelineEvidence("appl", provenance="kubernetes_loader_log",
                                                 evidence_kind="live", **stages)})
    response = authenticated_client(world, provider).get(path)
    assert response.status_code == 200
    assert "SYNTHETIC_ROOT" not in response.text
