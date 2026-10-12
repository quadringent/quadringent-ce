"""GET /v2/diagnostics and /v2/diagnostics/export: read-only, organization-scoped."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from quadringent.diagnostics.model import parse_report
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.evidence import EvidenceStage, PipelineEvidence

UPDATED = datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc)
CANARY_HOST = "ibmi-canary.customer.invalid"
CANARY_USER = "CANARYSVC"


def seed(engine, org, *, prefix, detected_version=None, verification="declared_not_verified",
         readiness="ready", declared_state="live", attention_reason=None):
    with engine.begin() as c:
        c.execute(v2_schema.organizations.insert(), {"id": org, "name": f"Synthetic {org}"})
        c.execute(v2_schema.sources.insert(), {
            "id": f"{prefix}src", "org_id": org, "display_name": "Synthetic source", "ibmi_host": CANARY_HOST,
            "ibmi_user": CANARY_USER, "secret_ciphertext": "gAAAAA==", "detected_version": detected_version,
            "tls_fingerprint": "ab" * 32 if detected_version else None, "updated_at": UPDATED})
        c.execute(v2_schema.destinations.insert(), {
            "id": f"{prefix}dst", "org_id": org, "snowflake_account": "canary-account", "key_pair_ciphertext": "gAAAAA==",
            "setup_script": "-- setup", "verification_state": verification})
        c.execute(v2_schema.tables.insert(), {
            "id": f"{prefix}tbl", "source_id": f"{prefix}src", "schema_name": "SALESLIB", "table_name": "ORDERS",
            "readiness": readiness})
        c.execute(v2_schema.pipelines.insert(), {
            "id": f"{prefix}ppl", "table_id": f"{prefix}tbl", "destination_id": f"{prefix}dst",
            "declared_state": declared_state, "attention_reason": attention_reason})


class Provider:
    def __init__(self, evidence=None):
        self.evidence_by_id = evidence or {}
        self.calls = []

    def observe(self, pipeline_id):  # pragma: no cover - not used by diagnostics
        raise AssertionError("diagnostics must not refresh observations")

    def evidence(self, pipeline_id):
        self.calls.append(pipeline_id)
        return self.evidence_by_id.get(pipeline_id, PipelineEvidence(pipeline_id))


class ForbiddenExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"diagnostics must never call the executor ({name})")


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'diagnostics.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


def client_for(engine, org="default", provider=None, **kwargs):
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), org_id=org,
                        pipeline_observation_provider=provider or Provider(),
                        pipeline_executor=ForbiddenExecutor(), **kwargs)
    return TestClient(app)


def by_code(body, code, subject_ref=None):
    return [r for r in body["results"] if r["code"] == code and (subject_ref is None or r["subject"]["ref"] == subject_ref)]


def test_report_follows_the_v1_contract_and_is_scoped_to_the_control_plane(engine):
    seed(engine, "default", prefix="a")
    body = client_for(engine).get("/v2/diagnostics").json()
    report = parse_report(body)
    assert report.scope == "control_plane"
    assert body["overall"]["status"] in ("blocked", "incomplete")
    assert by_code(body, "control_plane.api")[0]["status"] == "passed"


def test_configured_credentials_are_not_authentication(engine):
    seed(engine, "default", prefix="a", detected_version=None)
    body = client_for(engine).get("/v2/diagnostics").json()
    assert by_code(body, "source.authentication")[0]["status"] == "unknown"
    assert by_code(body, "source.tls")[0]["status"] == "unknown"


def test_a_probe_confirmed_source_is_passed_with_its_observation_time(engine):
    seed(engine, "default", prefix="a", detected_version="V7R5M0")
    auth = by_code(client_for(engine).get("/v2/diagnostics").json(), "source.authentication")[0]
    assert auth["status"] == "passed" and auth["observed_at"] == "2026-10-12T18:30:00Z"
    assert "not re-tested" in auth["limits"]


@pytest.mark.parametrize("state,expected", [("verified", "passed"), ("failed", "failed"),
                                            ("declared_not_verified", "unknown")])
def test_destination_verification_maps_to_a_status(engine, state, expected):
    seed(engine, "default", prefix="a", verification=state)
    assert by_code(client_for(engine).get("/v2/diagnostics").json(), "destination.verification")[0]["status"] == expected


def test_table_not_ready_is_an_established_prerequisite_failure(engine):
    seed(engine, "default", prefix="a", readiness="not_journaled")
    item = by_code(client_for(engine).get("/v2/diagnostics").json(), "source_prerequisites.table")[0]
    assert item["status"] == "failed" and item["subject"]["kind"] == "table"
    assert "CL" in item["next_action"]


def live(state, at="2026-10-12T18:59:00Z"):
    return EvidenceStage(state=state, observed_at=at if state != "absent" else None, event_count=3 if state != "absent" else None)


def test_capture_delivery_and_reconciliation_stay_distinct(engine):
    seed(engine, "default", prefix="a")
    evidence = {"appl": PipelineEvidence("appl", provenance="kubernetes_loader_log", evidence_kind="live",
                                          collected_at="2026-10-12T19:00:00Z", reading=live("observed"),
                                          delivery=live("stale", "2026-10-12T17:00:00Z"), reconciliation=live("absent"))}
    body = client_for(engine, provider=Provider(evidence)).get("/v2/diagnostics").json()
    assert by_code(body, "capture.reading")[0]["status"] == "passed"
    delivery = by_code(body, "delivery.snowflake")[0]
    assert delivery["status"] == "failed" and delivery["since"] == "2026-10-12T17:00:00Z"
    assert by_code(body, "reconciliation.destination")[0]["status"] == "unknown"
    assert by_code(body, "evidence.freshness")[0]["status"] == "failed"
    assert body["overall"]["status"] == "blocked"


@pytest.mark.parametrize("kind", ["simulation", "historical"])
def test_synthetic_or_historical_evidence_is_never_passed(engine, kind):
    seed(engine, "default", prefix="a")
    evidence = {"appl": PipelineEvidence("appl", provenance="demo_fixture", evidence_kind=kind,
                                          reading=live("observed"), delivery=live("observed"))}
    body = client_for(engine, provider=Provider(evidence)).get("/v2/diagnostics").json()
    for code in ("capture.reading", "delivery.snowflake"):
        item = by_code(body, code)[0]
        assert item["status"] == "unknown" and item["evidence"]["evidence_kind"] == kind


def test_attention_pipeline_is_a_failure_with_its_reason_bounded(engine):
    seed(engine, "default", prefix="a", declared_state="attention", attention_reason="reader stopped " + "x" * 900)
    item = by_code(client_for(engine).get("/v2/diagnostics").json(), "pipeline.state")[0]
    assert item["status"] == "failed" and len(item["evidence"]["attention_reason"]) <= 300


def test_pipeline_filter_and_cross_organization_reads(engine):
    seed(engine, "default", prefix="a")
    seed(engine, "other", prefix="b")
    client = client_for(engine, org="default")
    body = client.get("/v2/diagnostics").json()
    refs = json.dumps(body)
    assert "bppl" not in refs and "bsrc" not in refs and "bdst" not in refs
    assert client.get("/v2/diagnostics", params={"pipeline_id": "bppl"}).status_code == 404
    assert client.get("/v2/diagnostics", params={"pipeline_id": "missing"}).status_code == 404
    filtered = client.get("/v2/diagnostics", params={"pipeline_id": "appl"}).json()
    assert {r["subject"]["ref"] for r in filtered["results"] if r["subject"] and r["subject"]["kind"] == "pipeline"} == {"appl"}


def test_reading_diagnostics_changes_nothing(engine):
    seed(engine, "default", prefix="a", declared_state="attention", attention_reason="synthetic")

    def snapshot():
        with engine.connect() as c:
            return (c.execute(select(v2_schema.pipelines)).mappings().all(),
                    c.execute(select(v2_schema.sources)).mappings().all(),
                    c.execute(select(func.count()).select_from(v2_schema.audit_records)).scalar())

    before = snapshot()
    client = client_for(engine)
    client.get("/v2/diagnostics")
    client.get("/v2/diagnostics/export")
    assert snapshot() == before


def test_routes_require_an_identity_when_authentication_is_required(engine):
    seed(engine, "default", prefix="a")
    client = client_for(engine, token_pepper=b"synthetic-pepper", require_authentication=True)
    assert client.get("/v2/diagnostics").status_code == 401
    assert client.get("/v2/diagnostics/export").status_code == 401


def test_export_pseudonymizes_identities_and_previews_its_files(engine):
    seed(engine, "default", prefix="a", detected_version="V7R5M0")
    document = client_for(engine).get("/v2/diagnostics/export").json()
    assert document["schema"] == "quadringent.diagnostic-export.v1"
    text = json.dumps(document)
    for value in (CANARY_HOST, CANARY_USER, "canary-account", "appl", "asrc", "adst", "SALESLIB"):
        assert value not in text
    assert {f["name"] for f in document["preview"]["files"]} == set(document["files"])
