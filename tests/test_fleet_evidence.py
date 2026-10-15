"""Collecte du relevé pilote : des documents persistés aux mesures domaine."""

from __future__ import annotations

import unittest
from copy import deepcopy

import pytest

import site_fixture

site_fixture.build_test_site()

from quadringent_control_plane.fleet import ENVIRONMENT
from quadringent_control_plane.fleet_evidence import collect_fleet_evidence
from quadringent_control_plane.fleet_plan import (
    ATTACHED_STATUS,
    CONTINUITY_PROVEN,
    SOURCE_SCHEMA,
    parse_fleet_catalog,
)


def _column() -> dict[str, object]:
    return {
        "name": "COL1",
        "type": "CHAR",
        "length": 10,
        "numeric_precision": None,
        "numeric_scale": None,
        "ccsid": None,
        "nullable": False,
        "ordinal": 1,
    }


CATALOG_DOCUMENT = {
    "format_version": "quadringent-fleet-catalog-v1",
    "observed_at": "2026-09-18T08:00:00Z",
    "environment": ENVIRONMENT,
    "source_schema": SOURCE_SCHEMA,
    "journals": [
        {
            "library": "JRNLIB",
            "name": "DEMOJRN",
            "continuity": CONTINUITY_PROVEN,
            "receivers": [
                {
                    "library": "JRNLIB",
                    "name": "DEMOJRN1",
                    "status": "ONLINE",
                    "first_sequence": 1,
                    "last_sequence": 100,
                    "attach_timestamp": "2026-09-18T08:00:00Z",
                    "detach_timestamp": "2026-09-18T09:00:00Z",
                    "previous_library": None,
                    "previous_name": None,
                },
                {
                    "library": "JRNLIB",
                    "name": "DEMOJRN2",
                    "status": ATTACHED_STATUS,
                    "first_sequence": 101,
                    "last_sequence": 250,
                    "attach_timestamp": "2026-09-18T09:00:00Z",
                    "detach_timestamp": None,
                    "previous_library": "JRNLIB",
                    "previous_name": "DEMOJRN1",
                },
            ],
        }
    ],
    "tables": [
        {
            "name": name,
            "row_count": 1000 + index,
            "data_size": 2048,
            "member_count": 1,
            "journal_library": "JRNLIB",
            "journal_name": "DEMOJRN",
            "journal_images": "*AFTER",
            "columns": [_column()],
            "constraints": [],
            "indexes": [],
        }
        for index, name in enumerate(
            [
                "ADDRS1", "CAL001", "COST1", "CUSTOM1", "ORDER", "EXPENS",
                "DATE01", "SALE", "PLACE01", "PLACES", "CNTR", "PRODUCT",
                "HOLIDAYS",
            ]
        )
    ],
}


def _prepare() -> dict:
    return {
        "phase": "PREPARED",
        "intent_id": "intent-1",
        "checkpoint": {"receiver": "DEMOJRN1", "sequence": 10},
    }


def _snapshot() -> dict:
    return {
        "position": {"checkpoint": {"receiver": "DEMOJRN2", "sequence": 200}},
    }


def _proof() -> dict:
    return {
        "destination": {
            "tables": {
                "SALE": {
                    "snapshot_rows": 50,
                    "snapshot_published": 50,
                    "journal_rows": 5,
                    "raw_rows": 55,
                }
            }
        },
    }


def _certify(table: str = "SALE", **overrides) -> dict:
    """Document sonde conforme ``quadringent-certify-proof-v1``."""

    document = {
        "format_version": "quadringent-certify-proof-v1",
        "table": table,
        "measured_at": "2026-09-20T10:05:00+00:00",
        "window": {
            "start_utc": "2026-09-20T10:00:00+00:00",
            "end_utc": "2026-09-20T10:05:00+00:00",
        },
        "source_count": 55,
        "target_count": 55,
        "missing": 0,
        "extra": 0,
        "duplicates": 0,
        "source_hash": "777",
        "target_hash": "777",
        "destination_freshness_seconds": 0.0,
        "latency_seconds": 4.0,
        "throughput_rows_per_second": 0.02,
        "operations": {"SNAPSHOT_ROW": 50, "ADD_ROW1": 5},
    }
    document.update(overrides)
    return document


def financial_document():
    from dataclasses import replace
    from quadringent_control_plane.fleet_evidence import _credit_work_proof
    from test_fleet_progression import credit

    measured = replace(credit("measured", 0.15, table="SALE", intent="intent-1"),
                       work_proof=_credit_work_proof(_certify(), "intent-1", "SALE"))
    return {"format_version": "quadringent-fleet-credit-evidence-v1", "environment": ENVIRONMENT,
            "prepare_intent_id": "intent-1", "budget": credit("budget", 10).to_dict(),
            "tables": {"SALE": {"estimate": credit("estimate", 0.2).to_dict(),
                                 "measured": measured.to_dict()}}}


def test_explicit_financial_evidence_keeps_counts_and_credit_provenance_separate():
    finance = financial_document()
    result = collect_fleet_evidence(prepare_document=_prepare(), history_document={"phase": "HISTORICAL"},
        pause_document=None, catalog=parse_fleet_catalog(CATALOG_DOCUMENT), proof_document=_proof(),
        financial_document=finance, certify_documents={"SALE": _certify()})
    assert result.credit_budget.credits == 10
    assert result.tables["SALE"].estimated_credits.credits == 0.2
    assert result.tables["SALE"].measured_credits.credits == 0.15
    assert result.tables["SALE"].measured_credits.source == "attributed_metering"
    assert result.tables["SALE"].loaded_rows == 55 and result.tables["SALE"].estimated_rows == 1007


def test_catalog_and_raw_counts_never_create_financial_evidence():
    result = collect_fleet_evidence(prepare_document=_prepare(), history_document={"phase": "HISTORICAL"},
        pause_document=None, catalog=parse_fleet_catalog(CATALOG_DOCUMENT), proof_document=_proof())
    assert result.credit_budget is None
    assert all(m.estimated_credits is None and m.measured_credits is None for m in result.tables.values())


@pytest.mark.parametrize("change", ["other_intent", "other_environment", "no_provenance", "nan", "negative", "wrong_kind", "no_window"])
def test_unbound_or_invalid_financial_document_stays_unknown(change):
    finance = deepcopy(financial_document())
    if change == "other_intent":
        finance["prepare_intent_id"] = "another-intent"
    elif change == "other_environment":
        finance["environment"] = "other"
    else:
        measurement = finance["tables"]["SALE"]["measured"]
        if change == "no_provenance":
            measurement["evidence_sha256"] = None
        elif change == "nan":
            measurement["credits"] = float("nan")
        elif change == "negative":
            measurement["credits"] = -1
        elif change == "wrong_kind":
            measurement["kind"] = "estimate"
        elif change == "no_window":
            measurement["window"] = None
    result = collect_fleet_evidence(prepare_document=_prepare(), history_document={"phase": "HISTORICAL"},
        pause_document=None, catalog=None, financial_document=finance)
    assert result.tables["SALE"].measured_credits is None


class CollectEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = parse_fleet_catalog(CATALOG_DOCUMENT)

    def test_no_prepare_document_means_no_evidence(self) -> None:
        self.assertIsNone(
            collect_fleet_evidence(
                prepare_document=None,
                history_document={"phase": "HISTORICAL"},
                pause_document=None,
                catalog=self.catalog,
                snapshot_document=_snapshot(),
                proof_document=_proof(),
            )
        )

    def test_unprepared_phase_means_no_evidence(self) -> None:
        self.assertIsNone(
            collect_fleet_evidence(
                prepare_document={"phase": "PREPARING"},
                history_document=None,
                pause_document=None,
                catalog=self.catalog,
                proof_document=None,
            )
        )

    def test_full_evidence_maps_every_source(self) -> None:
        evidence = collect_fleet_evidence(
            prepare_document=_prepare(),
            history_document={"phase": "HISTORICAL"},
            pause_document={"phase": "PAUSED"},
            catalog=self.catalog,
            snapshot_document=_snapshot(),
            proof_document=_proof(),
        )

        self.assertEqual(evidence.prepare_intent_id, "intent-1")
        self.assertEqual(
            evidence.start_checkpoint.receiver, "DEMOJRN1"
        )
        self.assertTrue(evidence.history_active)
        self.assertTrue(evidence.paused)
        # Tail commis = last_sequence - 1 du receiver attaché.
        self.assertEqual(evidence.committed_tail.sequence, 249)
        self.assertEqual(evidence.committed_tail.receiver, "DEMOJRN2")
        self.assertTrue(evidence.continuity_proven)
        self.assertFalse(evidence.gap)
        self.assertEqual(evidence.current_checkpoint.sequence, 200)
        sale = evidence.tables["SALE"]
        self.assertEqual(sale.snapshot_rows, 50)
        self.assertEqual(sale.snapshot_published, 50)
        self.assertEqual(sale.loaded_rows, 55)
        self.assertEqual(sale.estimated_rows, 1007)
        # Une voie sans mesure destination garde son estimation catalogue.
        pays = evidence.tables["CNTR"]
        self.assertIsNone(pays.snapshot_rows)
        self.assertEqual(pays.estimated_rows, 1010)

    def test_missing_catalog_and_proof_leave_measures_unknown(self) -> None:
        evidence = collect_fleet_evidence(
            prepare_document=_prepare(),
            history_document={"phase": "HISTORICAL"},
            pause_document=None,
            catalog=None,
            proof_document=None,
        )

        self.assertIsNone(evidence.committed_tail)
        self.assertIsNone(evidence.receiver_chain)
        self.assertIsNone(evidence.continuity_proven)
        self.assertIsNone(evidence.current_checkpoint)
        self.assertTrue(all(m.estimated_rows is None for m in evidence.tables.values()))

    def test_certify_document_maps_to_measure(self) -> None:
        evidence = collect_fleet_evidence(
            prepare_document=_prepare(),
            history_document={"phase": "HISTORICAL"},
            pause_document=None,
            catalog=self.catalog,
            proof_document=_proof(),
            certify_documents={"SALE": _certify()},
        )

        cert = evidence.tables["SALE"].certification
        self.assertIsNotNone(cert)
        self.assertEqual(cert.source_count, 55)
        self.assertEqual(cert.source_hash, "777")
        self.assertEqual(cert.window.start_utc, "2026-09-20T10:00:00+00:00")
        self.assertEqual(cert.measured_at, "2026-09-20T10:05:00+00:00")
        # Les voies sans document gardent une certification inconnue.
        self.assertIsNone(evidence.tables["CNTR"].certification)

    def test_certify_document_outside_contract_is_ignored(self) -> None:
        evidence = collect_fleet_evidence(
            prepare_document=_prepare(),
            history_document={"phase": "HISTORICAL"},
            pause_document=None,
            catalog=self.catalog,
            certify_documents={
                "SALE": {"format_version": "other"},
                "CNTR": _certify(table="SALE"),  # voie déclarée ≠ clé
                "NOPE": _certify(table="SALE"),  # hors manifeste
            },
        )

        self.assertTrue(
            all(m.certification is None for m in evidence.tables.values())
        )

    def test_certify_document_unaligned_window_is_ignored(self) -> None:
        document = _certify()
        document["window"]["end_utc"] = "2026-09-20T10:05:00.5+00:00"
        evidence = collect_fleet_evidence(
            prepare_document=_prepare(),
            history_document={"phase": "HISTORICAL"},
            pause_document=None,
            catalog=self.catalog,
            certify_documents={"SALE": document},
        )

        self.assertIsNone(evidence.tables["SALE"].certification)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("change", ["no_proof", "changed_proof", "wrong_intent", "wrong_table", "missing_binding", "old_window", "observed_before_proof"])
def test_financial_measurement_requires_original_current_table_proof(change):
    from copy import deepcopy

    finance = deepcopy(financial_document())
    proofs = {"SALE": _certify()}
    measured = finance["tables"]["SALE"]["measured"]
    if change == "no_proof":
        proofs = None
    elif change == "changed_proof":
        proofs["SALE"]["source_hash"] = "another-source"
    elif change == "wrong_intent":
        measured["work_proof"]["prepare_intent_id"] = "previous-intent"
    elif change == "wrong_table":
        measured["work_proof"]["table_name"] = "ORDER"
    elif change == "missing_binding":
        measured.pop("work_proof")
    elif change == "old_window":
        measured["window"] = {"start_utc": "2000-01-01T00:00:00Z", "end_utc": "2000-01-02T00:00:00Z"}
    elif change == "observed_before_proof":
        measured["window"] = {"start_utc": "2000-01-01T00:00:00Z", "end_utc": "2000-01-02T00:00:00Z"}
        measured["observed_at"] = "2000-01-03T00:00:00Z"
    result = collect_fleet_evidence(prepare_document=_prepare(), history_document={"phase": "HISTORICAL"},
        pause_document=None, catalog=None, financial_document=finance, certify_documents=proofs)
    assert result.credit_budget.credits == 10
    assert result.tables["SALE"].estimated_credits.credits == 0.2
    assert result.tables["SALE"].measured_credits is None


def test_canonical_work_proof_hash_is_distinct_from_original_metering_hash():
    import hashlib
    import json

    finance = financial_document()
    result = collect_fleet_evidence(prepare_document=_prepare(), history_document={"phase": "HISTORICAL"},
        pause_document=None, catalog=None, financial_document=finance, certify_documents={"SALE": _certify()})
    measured = result.tables["SALE"].measured_credits
    expected = hashlib.sha256(json.dumps(_certify(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert measured.evidence_sha256 == "a" * 64
    assert measured.work_proof.canonical_sha256 == expected and expected != measured.evidence_sha256
