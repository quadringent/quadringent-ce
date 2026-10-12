"""Backend assertions of the local laboratory matrix (scenarios 0-20).

Each ``test_scenario_NN_*`` runs the real v2 application (``create_v2_app``
over a throwaway SQLite file) through the laboratory gateway, with IBM i,
Snowflake and Kubernetes replaced only at the product's injectable
boundaries. No external connection is made. The matrix and its limits are in
``docs/product/local-lab.md``; browser checks are in
``ui/scripts/lab-browser-scenarios.mjs``.

Assertions on ``/v2/diagnostics`` activate automatically once the
coordinator wires that route into ``create_v2_app``; until then they skip
with an explicit reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from quadringent_lab import fixtures
from quadringent_lab.faults import FaultRule
from quadringent_lab.runtime import LabRuntime
from quadringent_lab.gateway import LabGateway
from quadringent_lab.server import ThreadedLab

from test_lab_support import DIAGNOSTICS_SKIP_REASON, REPO_ROOT, Lab, build_lab, key


@pytest.fixture()
def lab_for(tmp_path):
    created: list[Lab] = []

    def factory(scenario: str) -> Lab:
        workdir = tmp_path / f"lab{len(created)}"
        workdir.mkdir()
        lab = build_lab(workdir, scenario)
        created.append(lab)
        return lab

    yield factory
    for lab in created:
        lab.runtime.close()


def _need_diagnostics(lab: Lab) -> None:
    if not lab.diagnostics_available():
        pytest.skip(DIAGNOSTICS_SKIP_REASON)


def _diagnostics(session, **params) -> dict:
    response = session.get("/v2/diagnostics", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _results(report: dict, code: str) -> list[dict]:
    return [result for result in report["results"] if result["code"] == code]


# --- 0: synthetic baseline ----------------------------------------------------
def test_scenario_00_baseline(lab_for):
    lab = lab_for("baseline")
    operator = lab.session()
    evidence = operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").json()
    assert evidence["evidence_kind"] == "simulation"
    assert evidence["provenance"] == "lab_fixture"
    assert {evidence[stage]["state"] for stage in ("reading", "delivery", "reconciliation")} == {"observed"}
    items = operator.get("/v2/pipelines").json()["items"]
    assert fixtures.PIPELINE_ORDERS in {item["id"] for item in items}
    # No live evidence anywhere in the baseline.
    for pipeline_id in (fixtures.PIPELINE_ORDERS, fixtures.PIPELINE_CUSTOMERS):
        assert operator.get(f"/v2/pipelines/{pipeline_id}/evidence").json()["evidence_kind"] != "live"


# --- 1: environment ready --------------------------------------------------------
def test_scenario_01_environment_ready(lab_for):
    lab = lab_for("environment_ready")
    operator = lab.session()
    source = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}").json()
    assert source["detected_version"] == "V7R5M0"
    assert source["tls_fingerprint"]
    assert operator.get(f"/v2/destinations/{fixtures.DESTINATION_ID}").json()["verification_state"] == "verified"
    tables = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}/tables").json()["items"]
    assert {table["readiness"] for table in tables} == {"ready"}
    evidence = operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").json()
    # "live" is a laboratory-injected condition, labelled as such in the detail.
    assert evidence["evidence_kind"] == "live"
    assert evidence["provenance"] == "lab_fixture"
    assert "Laboratory-injected" in evidence["reading"]["detail"]
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert report["overall"]["status"] == "ready", report["overall"]
        assert report["overall"]["failed"] == 0


# --- 2: tool missing (CLI only) ----------------------------------------------------
def test_scenario_02_tool_missing(lab_for):
    lab = lab_for("tool_missing")
    from quadringent_lab.scenarios import get_scenario

    assert get_scenario("tool_missing").coverage == "cli"
    if lab.diagnostics_available():
        # The control plane never inspects local tools: that is the CLI's job.
        report = _diagnostics(lab.session())
        assert not [result for result in report["results"] if result["layer"] == "local_tools"]
    doctor_tests = sorted((REPO_ROOT / "tests").glob("test_doctor*.py"))
    if not doctor_tests:
        pytest.skip("covered by CLI tests (tests/test_doctor*.py, coordinator-owned) which do not exist yet")


# --- 3-5: source probe ----------------------------------------------------------------
def _test_source(operator) -> dict:
    response = operator.post(f"/v2/sources/{fixtures.SOURCE_ID}/test")
    assert response.status_code == 200, response.text
    assert fixtures.SOURCE_SECRET not in response.text
    return response.json()["after"]


def test_scenario_03_tcp_unreachable(lab_for):
    lab = lab_for("tcp_unreachable")
    operator = lab.session()
    result = _test_source(operator)
    assert result["reachable"] is False
    assert result["network"]["ok"] is False
    assert result["tls"]["ok"] is False and result["authentication"]["ok"] is False
    assert "Not attempted" in result["tls"]["detail"]
    source = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}").json()
    assert source["tls_fingerprint"] is None and source["detected_version"] is None
    assert len(lab.runtime.state.ledger("source_probe")) == 1
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert _results(report, "source.authentication")[0]["status"] == "unknown"


def test_scenario_04_tls_invalid(lab_for):
    lab = lab_for("tls_invalid")
    operator = lab.session()
    result = _test_source(operator)
    assert result["reachable"] is False
    assert result["network"]["ok"] is True
    assert result["tls"]["ok"] is False
    assert result["tls"]["trust"] == "unknown"
    assert result["tls"]["fingerprint"]  # measured, offered for explicit comparison
    assert result["authentication"]["ok"] is False
    source = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}").json()
    # A failed TLS step never persists trust or a fingerprint.
    assert source["tls_fingerprint"] is None and source["tls_trust"] is None
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert _results(report, "source.tls")[0]["status"] == "unknown"


def test_scenario_05_auth_denied(lab_for):
    lab = lab_for("auth_denied")
    operator = lab.session()
    result = _test_source(operator)
    assert result["reachable"] is False
    assert result["tls"]["ok"] is True
    assert result["authentication"]["ok"] is False
    source = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}").json()
    assert source["detected_version"] is None
    assert source["tls_fingerprint"] is None  # only a fully reachable probe persists these
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert _results(report, "source.authentication")[0]["status"] == "unknown"


# --- 6-8: table readiness --------------------------------------------------------------
def _tables_by_name(operator) -> dict[str, dict]:
    response = operator.post(f"/v2/sources/{fixtures.SOURCE_ID}/tables/refresh")
    assert response.status_code == 200, response.text
    return {table["table_name"]: table for table in response.json()["after"]}


def test_scenario_06_journal_absent(lab_for):
    lab = lab_for("journal_absent")
    operator = lab.session()
    tables = _tables_by_name(operator)
    assert tables["LEDGER_RAW"]["readiness"] == "not_journaled"
    commands = " ".join(item["command"] for item in tables["LEDGER_RAW"]["cl_fix_commands"])
    assert "STRJRNPF" in commands
    assert tables["ORDERS"]["readiness"] == "ready"
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        failed = [r for r in _results(report, "source_prerequisites.table") if r["status"] == "failed"]
        assert [r["subject"]["label"] for r in failed] == ["LABLIB.LEDGER_RAW"] or len(failed) == 1
        assert report["overall"]["status"] == "blocked"


def test_scenario_07_journal_config_incompatible(lab_for):
    lab = lab_for("journal_config_incompatible")
    operator = lab.session()
    tables = _tables_by_name(operator)
    assert tables["LEDGER_AFTER"]["readiness"] == "images_incomplete"
    assert any("CHGJRNOBJ" in item["command"] for item in tables["LEDGER_AFTER"]["cl_fix_commands"])
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert any(r["status"] == "failed" for r in _results(report, "source_prerequisites.table"))


def test_scenario_08_no_usable_key(lab_for):
    lab = lab_for("no_usable_key")
    operator = lab.session()
    tables = _tables_by_name(operator)
    table = tables["LEDGER_NOKEY"]
    assert table["readiness"] == "no_key"
    refused = operator.patch(
        f"/v2/tables/{table['id']}", {"key_strategy": "rrn", "key_columns": None, "acknowledge_rrn": False}
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_request"
    still = operator.get(f"/v2/sources/{fixtures.SOURCE_ID}/tables").json()["items"]
    assert {item["table_name"]: item["readiness"] for item in still}["LEDGER_NOKEY"] == "no_key"
    accepted = operator.patch(
        f"/v2/tables/{table['id']}", {"key_strategy": "rrn", "key_columns": None, "acknowledge_rrn": True}
    )
    assert accepted.status_code == 200
    assert accepted.json()["after"]["readiness"] == "ready"
    assert accepted.json()["after"]["key_strategy"] == "rrn"


# --- 9: destination privileges -----------------------------------------------------------
def test_scenario_09_destination_privileges(lab_for):
    lab = lab_for("destination_privileges")
    operator = lab.session()
    response = operator.post(f"/v2/destinations/{fixtures.DESTINATION_ID}/verify")
    assert response.status_code == 200, response.text
    probe = response.json()["after"]
    assert probe["verified"] is False
    assert probe["load_privileges"]["ok"] is False
    assert probe["connection"]["ok"] is True
    assert "BEGIN PRIVATE KEY" not in response.text
    destination = operator.get(f"/v2/destinations/{fixtures.DESTINATION_ID}").json()
    assert destination["verification_state"] == "failed"
    if lab.diagnostics_available():
        report = _diagnostics(operator)
        assert _results(report, "destination.verification")[0]["status"] == "failed"
    # Fixing the grants (condition flipped) and verifying again recovers.
    lab.runtime.patch_conditions({"verifier": "ok"})
    again = operator.post(f"/v2/destinations/{fixtures.DESTINATION_ID}/verify").json()["after"]
    assert again["verified"] is True
    assert operator.get(f"/v2/destinations/{fixtures.DESTINATION_ID}").json()["verification_state"] == "verified"


# --- 10: control plane down --------------------------------------------------------------
def test_scenario_10_control_plane_down(lab_for):
    lab = lab_for("control_plane_down")
    threaded = ThreadedLab(lab.gateway).start()
    try:
        assert httpx.get(f"{threaded.url}/v2/healthz", timeout=5).json() == {"status": "ok"}
    finally:
        threaded.stop()
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{threaded.url}/v2/healthz", timeout=2)


# --- 11-13: evidence states ---------------------------------------------------------------
def test_scenario_11_read_without_delivery(lab_for):
    lab = lab_for("read_without_delivery")
    operator = lab.session()
    evidence = operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").json()
    assert evidence["evidence_kind"] == "live"  # laboratory-injected
    assert evidence["reading"]["state"] == "observed"
    assert evidence["delivery"]["state"] == "absent" and evidence["delivery"]["observed_at"] is None
    assert evidence["reconciliation"]["state"] == "absent"
    if lab.diagnostics_available():
        report = _diagnostics(operator, pipeline_id=fixtures.PIPELINE_ORDERS)
        assert _results(report, "capture.reading")[0]["status"] == "passed"
        assert _results(report, "delivery.snowflake")[0]["status"] == "unknown"
        assert _results(report, "reconciliation.destination")[0]["status"] == "unknown"
        assert report["overall"]["status"] == "incomplete"


def test_scenario_12_evidence_absent(lab_for):
    lab = lab_for("evidence_absent")
    operator = lab.session()
    evidence = operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").json()
    assert evidence["evidence_kind"] == "absent"
    assert {evidence[stage]["state"] for stage in ("reading", "delivery", "reconciliation")} == {"absent"}
    pipeline = next(
        item for item in operator.get("/v2/pipelines").json()["items"] if item["id"] == fixtures.PIPELINE_ORDERS
    )
    assert pipeline["lag_seconds"] is None and pipeline["absent_reasons"]  # missing stays missing
    assert pipeline["declared_state"] == "live"  # the declared state is not evidence
    if lab.diagnostics_available():
        report = _diagnostics(operator, pipeline_id=fixtures.PIPELINE_ORDERS)
        for code in ("capture.reading", "delivery.snowflake", "reconciliation.destination"):
            assert _results(report, code)[0]["status"] == "unknown"
        assert report["overall"]["status"] != "ready"


def test_scenario_13_evidence_stale(lab_for):
    lab = lab_for("evidence_stale")
    operator = lab.session()
    evidence = operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").json()
    assert evidence["reading"]["state"] == "observed"
    assert evidence["delivery"]["state"] == "stale"
    observed = datetime.fromisoformat(evidence["delivery"]["observed_at"].replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - observed > timedelta(hours=2)
    if lab.diagnostics_available():
        report = _diagnostics(operator, pipeline_id=fixtures.PIPELINE_ORDERS)
        delivery = _results(report, "delivery.snowflake")[0]
        assert delivery["status"] == "failed" and delivery["since"]
        assert _results(report, "evidence.freshness")[0]["status"] == "failed"
        assert report["overall"]["status"] == "blocked"


# --- 14: late response of a former pipeline ------------------------------------------------
def test_scenario_14_late_old_pipeline_response(lab_for):
    lab = lab_for("late_old_pipeline_response")
    assert [rule["id"] for rule in lab.runtime.public_state()["faults"]] == ["late-orders-evidence"]
    threaded = ThreadedLab(lab.gateway).start()
    try:
        with httpx.Client(base_url=threaded.url, timeout=15) as client:
            client.post("/v2/auth/login", json={"email": fixtures.OPERATOR_EMAIL, "password": fixtures.LAB_PASSWORD})
            arrivals: dict[str, tuple[float, dict]] = {}
            started = time.monotonic()

            def fetch(pipeline_id: str) -> None:
                with httpx.Client(base_url=threaded.url, timeout=15, cookies=client.cookies) as worker:
                    body = worker.get(f"/v2/pipelines/{pipeline_id}/evidence").json()
                arrivals[pipeline_id] = (time.monotonic() - started, body)

            old = threading.Thread(target=fetch, args=(fixtures.PIPELINE_ORDERS,))
            old.start()
            time.sleep(0.2)
            fetch(fixtures.PIPELINE_CUSTOMERS)  # the operator moved on to another pipeline
            old.join(timeout=15)
    finally:
        threaded.stop()
    new_at, new_body = arrivals[fixtures.PIPELINE_CUSTOMERS]
    old_at, old_body = arrivals[fixtures.PIPELINE_ORDERS]
    assert new_at < old_at, "the former pipeline's answer must arrive later"
    assert old_at >= 2.5
    # Each payload names its own pipeline, so a client can discard the stale one.
    assert new_body["pipeline_id"] == fixtures.PIPELINE_CUSTOMERS
    assert old_body["pipeline_id"] == fixtures.PIPELINE_ORDERS
    assert "LABLIB.CUSTOMERS" in new_body["reading"]["detail"]
    assert "LABLIB.ORDERS" in old_body["reading"]["detail"]


# --- 15: action confirmed, effect read back ---------------------------------------------------
def test_scenario_15_action_confirmed_reread(lab_for):
    lab = lab_for("action_confirmed_reread")
    operator = lab.session()
    target = fixtures.PIPELINE_CUSTOMERS
    action = f"/v2/pipelines/{target}/actions/restart_initial_copy"

    # Dry run changes nothing and never reaches the executor.
    dry = operator.post(action, {"dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"]["would_transition"]["to"] == "copying"
    assert lab.executor_events() == []

    # A sensitive action without confirmation only creates a pending confirmation.
    refused = operator.post(action)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "pending_confirmation_required"
    assert lab.executor_events() == []
    pending = operator.get("/v2/confirmations").json()["items"]
    assert [item["resource_id"] for item in pending] == [target]
    confirmation = pending[0]
    assert confirmation["available"] == {"approve": True, "reject": True, "execute": False}

    approved = operator.post(f"/v2/confirmations/{confirmation['id']}/approve")
    assert approved.status_code == 200, approved.text
    executed = operator.post(action, {"confirmation_token": confirmation["id"]})
    assert executed.status_code == 200, executed.text
    assert executed.json()["verify"] == {"method": "GET", "path": f"/v2/pipelines/{target}"}
    # Read back: the effect is verified from the service, not assumed.
    reread = operator.get(f"/v2/pipelines/{target}").json()
    assert reread["declared_state"] == "copying"
    assert lab.executor_events(target) == ["restart_initial_copy"]
    used = operator.get(f"/v2/confirmations/{confirmation['id']}").json()
    assert used["state"] == "used"
    # The same confirmation cannot be replayed with a new idempotency key.
    replay = operator.post(action, {"confirmation_token": confirmation["id"]})
    assert replay.status_code == 403 and replay.json()["error"]["code"] == "wrong_confirmation"
    assert lab.executor_events(target) == ["restart_initial_copy"]


def test_scenario_15_executor_failure_is_not_a_success(lab_for):
    lab = lab_for("action_confirmed_reread")
    operator = lab.session()
    lab.runtime.patch_conditions({"executor": "unavailable"})
    response = operator.post(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/actions/pause")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "executor_unavailable"
    # The declared state was not changed by a failed execution.
    assert operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}").json()["declared_state"] == "live"
    lab.runtime.patch_conditions({"executor": "ok"})
    paused = operator.post(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/actions/pause")
    assert paused.status_code == 200
    assert operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}").json()["declared_state"] == "paused"


# --- 16: timeout, effect still unknown -----------------------------------------------------------
def test_scenario_16_timeout_effect_unknown(lab_for):
    lab = lab_for("timeout_effect_unknown")
    default_fault = lab.runtime.public_state()["faults"][0]
    assert default_fault["mode"] == "delay_after" and default_fault["seconds"] == 4.0
    # Same fault, shortened so the test stays quick.
    lab.runtime.faults.replace([FaultRule(**{**default_fault, "seconds": 2.0, "hits": 0})])
    target = fixtures.PIPELINE_ORDERS
    path = f"/v2/pipelines/{target}/actions/pause"
    idempotency_key = {"Idempotency-Key": "lab-timeout-key-1"}
    threaded = ThreadedLab(lab.gateway).start()
    try:
        with httpx.Client(base_url=threaded.url, timeout=httpx.Timeout(10, read=0.7)) as client:
            client.post("/v2/auth/login", json={"email": fixtures.OPERATOR_EMAIL, "password": fixtures.LAB_PASSWORD})
            with pytest.raises(httpx.ReadTimeout):
                client.post(path, json={}, headers=idempotency_key)
            # The client does not know the outcome. Reading it back reveals the effect.
            assert client.get(f"/v2/pipelines/{target}").json()["declared_state"] == "paused"
            assert lab.executor_events(target) == ["pause"]
            time.sleep(1.6)  # let the withheld response finish server side
            # Retrying with the same key replays the stored result: no second effect.
            replay = client.post(path, json={}, headers=idempotency_key)
            assert replay.status_code == 200
            assert replay.json()["after"]["declared_state"] == "paused"
            assert lab.executor_events(target) == ["pause"]
            # A fresh key would be a new mutation, now refused by the state machine.
            fresh = client.post(path, json={}, headers=key())
            assert fresh.status_code == 409
            assert lab.executor_events(target) == ["pause"]
    finally:
        threaded.stop()


# --- 17: user without rights -----------------------------------------------------------------------
def test_scenario_17_reader_without_rights(lab_for):
    lab = lab_for("reader_without_rights")
    operator = lab.session()
    # A pending confirmation exists so the reader's refusal to decide it is observable.
    operator.post(f"/v2/pipelines/{fixtures.PIPELINE_CUSTOMERS}/actions/restart_initial_copy")
    confirmation_id = operator.get("/v2/confirmations").json()["items"][0]["id"]
    reader = lab.session("reader")
    assert reader.get("/v2/pipelines").status_code == 200
    assert reader.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/evidence").status_code == 200
    listed = reader.get("/v2/confirmations").json()["items"][0]
    assert listed["available"] == {"approve": False, "reject": False, "execute": False}
    for response in (
        reader.post(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/actions/pause"),
        reader.post(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/actions/pause", {"dry_run": True}),
        reader.post(f"/v2/confirmations/{confirmation_id}/approve"),
        reader.post(f"/v2/sources/{fixtures.SOURCE_ID}/test"),
        reader.get("/v2/audit"),
        reader.get("/v2/users"),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "insufficient_role"
    assert lab.executor_events() == []
    assert operator.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}").json()["declared_state"] == "live"
    # Without any identity the service answers 401, never an anonymous admin.
    assert lab.anonymous().get("/v2/pipelines").status_code == 401
    if lab.diagnostics_available():
        assert reader.get("/v2/diagnostics").status_code == 200  # read scope is enough


# --- 18: two organizations ------------------------------------------------------------------------------
def test_scenario_18_cross_organization(lab_for):
    lab = lab_for("cross_organization")
    operator = lab.session()
    other = lab.session("other")
    operator.post(f"/v2/pipelines/{fixtures.PIPELINE_CUSTOMERS}/actions/restart_initial_copy")
    lab_confirmation = operator.get("/v2/confirmations").json()["items"][0]["id"]

    # Organization-scoped resources: destinations, users, confirmations, audit.
    assert [d["id"] for d in other.get("/v2/destinations").json()["items"]] == [fixtures.OTHER_DESTINATION_ID]
    assert other.get(f"/v2/destinations/{fixtures.DESTINATION_ID}").status_code == 404
    assert other.get(f"/v2/destinations/{fixtures.DESTINATION_ID}/setup-script").status_code == 404
    assert [u["email"] for u in other.get("/v2/users").json()["items"]] == [fixtures.OTHER_ADMIN_EMAIL]
    assert other.get("/v2/confirmations?state=all").json()["items"] == []
    assert other.get(f"/v2/confirmations/{lab_confirmation}").status_code == 404
    assert other.post(f"/v2/confirmations/{lab_confirmation}/approve").status_code == 404
    assert [r for r in other.get("/v2/audit").json()["items"] if r["actor_id"] != fixtures.USER_IDS[fixtures.OTHER_ADMIN_EMAIL]] == []
    assert other.post(f"/v2/destinations/{fixtures.DESTINATION_ID}/verify").status_code == 404

    # A session never crosses the organization boundary.
    stolen = lab.anonymous()
    stolen.client.cookies.set(
        "quadringent_session", operator.client.cookies["quadringent_session"], domain="127.0.0.1"
    )
    stolen.prefix = "/lab-other"
    assert stolen.get("/v2/pipelines").status_code == 401
    back = lab.anonymous()
    back.client.cookies.set("quadringent_session", other.client.cookies["quadringent_session"], domain="127.0.0.1")
    assert back.get("/v2/pipelines").status_code == 401
    assert lab.executor_events() == []

    if lab.diagnostics_available():
        assert other.get("/v2/diagnostics", params={"pipeline_id": fixtures.PIPELINE_ORDERS}).status_code == 404
        report = other.get("/v2/diagnostics").json()
        text = json.dumps(report)
        for leaked in (fixtures.SOURCE_ID, fixtures.DESTINATION_ID, fixtures.PIPELINE_ORDERS, "LABLIB"):
            assert leaked not in text
        mine = lab.session().get("/v2/diagnostics").json()
        assert fixtures.PIPELINE_OTHER_ORDERS not in json.dumps(mine)


_XFAIL_ORG = (
    "existing defect, returned to the coordinator: sources/tables/pipelines services do not filter by organization "
    "(the pipelines table has no org_id and queries do not join sources.org_id); see local-lab.md limits"
)


def test_scenario_18_pipelines_list_is_organization_scoped(lab_for):
    other = lab_for("cross_organization").session("other")
    ids = {item["id"] for item in other.get("/v2/pipelines").json()["items"]}
    assert ids == {fixtures.PIPELINE_OTHER_ORDERS}


def test_scenario_18_pipeline_detail_is_organization_scoped(lab_for):
    other = lab_for("cross_organization").session("other")
    assert other.get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}").status_code == 404


def test_scenario_18_actions_cannot_target_another_organization(lab_for):
    lab = lab_for("cross_organization")
    # The main organization's operator tries to pause the other organization's pipeline.
    response = lab.session().post(f"/v2/pipelines/{fixtures.PIPELINE_OTHER_ORDERS}/actions/pause")
    assert response.status_code == 404
    assert lab.executor_events(fixtures.PIPELINE_OTHER_ORDERS) == []


def test_scenario_18_sources_are_organization_scoped(lab_for):
    other = lab_for("cross_organization").session("other")
    ids = {item["id"] for item in other.get("/v2/sources").json()["items"]}
    assert ids == {fixtures.OTHER_SOURCE_ID}


# --- 19: export with canaries ----------------------------------------------------------------------------
def test_scenario_19_logs_redact_canaries(lab_for):
    lab = lab_for("export_canaries")
    response = lab.session().get(f"/v2/pipelines/{fixtures.PIPELINE_ORDERS}/logs")
    assert response.status_code == 200
    assert fixtures.CANARY_LOG_SECRET not in response.text
    assert fixtures.CANARY_ROW_VALUE not in response.text
    assert "[SECRET_MASQUE]" in response.text  # redaction is applied by the service


def test_scenario_19_export_canaries(lab_for):
    lab = lab_for("export_canaries")
    _need_diagnostics(lab)
    reader = lab.session("reader")
    response = reader.get("/v2/diagnostics/export")
    assert response.status_code == 200, response.text
    export = response.json()
    assert export["schema"] == "quadringent.diagnostic-export.v1"
    corpus = json.dumps(export)
    forbidden = list(fixtures.EXPORT_FORBIDDEN_CANARIES)
    forbidden.append(lab.runtime.seed_result.generated["agent_token"])
    forbidden.append("PRIVATE KEY")
    forbidden.append(fixtures.CANARY_TABLE_COMMENT)
    for canary in forbidden:
        assert canary not in corpus, f"canary leaked into the export: {canary[:24]}"
    # The preview describes the files and what was omitted, before any download.
    preview = export["preview"]
    assert preview["omitted"] and preview["limits"]
    for entry in preview["files"]:
        text = export["files"][entry["name"]]
        assert entry["bytes"] == len(text.encode("utf-8"))
        assert entry["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Reading the report itself does not expose them either.
    assert fixtures.CANARY_SECRET not in reader.get("/v2/diagnostics").text


# --- 20: resume after refresh -------------------------------------------------------------------------------
def test_scenario_20_resume_after_refresh(lab_for):
    lab = lab_for("resume_after_refresh")
    operator = lab.session()
    target = fixtures.PIPELINE_CUSTOMERS
    action = f"/v2/pipelines/{target}/actions/restart_initial_copy"
    assert operator.post(action).status_code == 409
    confirmation_id = operator.get("/v2/confirmations").json()["items"][0]["id"]
    cookie = operator.client.cookies["quadringent_session"]

    # "Refresh": a brand-new runtime over the same database file, no reseed.
    restarted = LabRuntime(lab.runtime.workdir, scenario="resume_after_refresh", reseed=False)
    try:
        gateway = LabGateway(restarted)
        resumed = Lab(runtime=restarted, gateway=gateway).anonymous()
        # The session cookie issued before the restart is still valid (stateless signature).
        resumed.client.cookies.set("quadringent_session", cookie, domain="127.0.0.1")
        pending = resumed.get("/v2/confirmations").json()["items"]
        assert [item["id"] for item in pending] == [confirmation_id]
        assert resumed.get(f"/v2/pipelines/{target}").json()["declared_state"] == "copying"
        assert resumed.post(f"/v2/confirmations/{confirmation_id}/approve").status_code == 200
        done = resumed.post(action, {"confirmation_token": confirmation_id})
        assert done.status_code == 200, done.text
        assert [e["event"] for e in restarted.state.ledger("pipeline_executor")] == ["restart_initial_copy"]
    finally:
        restarted.close()
