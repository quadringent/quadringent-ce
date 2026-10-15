"""Diagnostic contract quadringent.diagnostic.v1: shared by the CLI, the API and the cockpit."""

from __future__ import annotations

import json

import pytest

from quadringent.diagnostics.model import (
    EXIT_BLOCKED,
    EXIT_INCOMPLETE,
    EXIT_READY,
    SCHEMA,
    DiagnosticContractError,
    DiagnosticReport,
    DiagnosticResult,
    Subject,
    parse_report,
)

NOW = "2026-10-12T19:00:00Z"


def result(code="network.tcp.database", layer="network", status="passed", required=True, **extra):
    return DiagnosticResult(
        code=code, layer=layer, status=status, required=required, observed_at=NOW,
        provenance="probe:tcp", summary="Synthetic summary.", next_action="Synthetic action.",
        limits="Synthetic limits.", **extra)


def report(*results, synthetic=False):
    return DiagnosticReport(results=tuple(results), generated_at=NOW, scope="local", synthetic=synthetic,
                            software={"product": "quadringent", "version": "0.3.5", "distribution": "checkout"})


def test_a_failed_result_blocks_whatever_else_passed():
    built = report(result(status="passed"), result(code="tls.database", layer="tls", status="failed"))
    assert built.overall()["status"] == "blocked"
    assert built.exit_code() == EXIT_BLOCKED


def test_an_unknown_required_layer_is_never_ready():
    built = report(result(status="passed"), result(code="authentication.signon", layer="authentication", status="unknown"))
    assert built.overall()["status"] == "incomplete"
    assert built.exit_code() == EXIT_INCOMPLETE
    assert "healthy" not in json.dumps(built.to_dict()).lower()


def test_a_skipped_required_layer_is_never_ready():
    assert report(result(status="skipped")).overall()["status"] == "incomplete"


def test_optional_unknowns_do_not_block_a_ready_report():
    built = report(result(status="passed"), result(code="local_config.cloud", layer="local_config", status="unknown", required=False))
    assert built.overall()["status"] == "ready"
    assert built.exit_code() == EXIT_READY


def test_an_empty_report_is_incomplete_not_ready():
    assert report().overall()["status"] == "incomplete"


def test_counts_cover_every_status():
    built = report(result(status="passed"), result(status="failed"), result(status="unknown"), result(status="skipped"))
    assert {k: built.overall()[k] for k in ("passed", "failed", "unknown", "skipped")} == {
        "passed": 1, "failed": 1, "unknown": 1, "skipped": 1}


def test_round_trip_through_json_keeps_every_field():
    original = report(result(subject=Subject("pipeline", "pl_1", "SALES.ORDERS"), since="2026-10-12T18:40:00Z",
                             evidence={"port": 9471, "stage_state": "stale", "live": False, "count": None}))
    parsed = parse_report(json.loads(json.dumps(original.to_dict())))
    assert parsed.to_dict() == original.to_dict()


@pytest.mark.parametrize("field,value", [
    ("status", "healthy"), ("layer", "magic"), ("observed_at", "2026-10-12 19:00"), ("code", "Not A Code"),
])
def test_values_outside_the_contract_are_rejected(field, value):
    document = report(result()).to_dict()
    document["results"][0][field] = value
    with pytest.raises(DiagnosticContractError):
        parse_report(document)


def test_evidence_only_holds_bounded_scalars():
    with pytest.raises(DiagnosticContractError):
        result(evidence={"nested": {"a": 1}})
    with pytest.raises(DiagnosticContractError):
        result(evidence={"text": "x" * 501})


def test_another_schema_is_refused():
    document = report(result()).to_dict()
    document["schema"] = "quadringent.diagnostic.v2"
    with pytest.raises(DiagnosticContractError):
        parse_report(document)


def test_a_v1_document_from_an_earlier_producer_still_parses():
    """Compatibility: additive fields are ignored, required fields are stable."""
    document = {
        "schema": SCHEMA, "generated_at": NOW, "scope": "control_plane", "synthetic": True,
        "software": {"product": "quadringent", "version": "0.3.5", "distribution": "control-plane"},
        "overall": {"status": "ready", "passed": 0, "failed": 0, "unknown": 0, "skipped": 0, "summary": "stale"},
        "results": [{
            "code": "delivery.snowflake", "layer": "delivery", "status": "unknown", "required": True,
            "observed_at": NOW, "provenance": "control-plane:evidence", "subject": None, "since": None,
            "summary": "s", "next_action": "a", "limits": "l", "evidence": {}, "future_field": "ignored",
        }],
        "future_top_level": 1,
    }
    parsed = parse_report(document)
    # The overall verdict is always recomputed, never trusted from the input.
    assert parsed.overall()["status"] == "incomplete"
