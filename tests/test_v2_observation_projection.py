"""Adaptateur d'observation réel — documents console/projection v1
(``v2/services/observation_projection.py``, chantier observabilité v2 suite).

Écrit un vrai document console au format ``as400-console-v1`` sur disque
(``file://``) et vérifie que l'adaptateur le lit via
``repository.ProjectionRepository`` sans réinventer son propre parseur —
états/retard/compteurs viennent exactement de ce que la projection v1
calcule, jamais d'une valeur substituée.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quadringent_control_plane.v2.services.observation_projection import (
    NO_MAPPING_REASON,
    ProjectionRepositoryObservationAdapter,
)

NOW = datetime.now(timezone.utc)


def _write_document(tmp_path, *, counters=None, lag_series=None, run_state="RUNNING") -> str:
    document = {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=5)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": run_state, "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {name: {"value": value} for name, value in (counters or {}).items()},
    }
    if lag_series is not None:
        document["lag"]["series"] = lag_series
    path = tmp_path / "console.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return f"file://{path}"


def test_unmapped_pipeline_returns_absent_with_reason() -> None:
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: None)
    observation = adapter.observe("ppl-unknown")
    assert observation.observed_state is None
    assert observation.absent_reasons["observed_state"] == NO_MAPPING_REASON


def test_evidence_is_bound_to_declared_pipeline_and_preserves_provenance(tmp_path):
    origin = _write_document(tmp_path, counters={"events_published": 120})
    adapter = ProjectionRepositoryObservationAdapter(
        lambda pipeline_id: f"simulation:demo:{origin}" if pipeline_id == "ppl1" else None
    )
    evidence = adapter.evidence("ppl1")
    assert evidence.evidence_kind == "simulation"
    assert evidence.reading.event_count == 120
    assert evidence.delivery.state == "absent"
    assert evidence.reconciliation.duplicate_count is None
    assert adapter.evidence("another-table").reading.event_count is None


def test_reconciliation_keeps_its_own_timestamp_and_journal_scope(tmp_path):
    from test_control_plane_projection import fresh_destination_proof_document

    now = datetime.now(timezone.utc)
    document = fresh_destination_proof_document()

    def refresh_dates(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"generated_at", "observed_at", "capture_observed_at"} and isinstance(item, str):
                    value[key] = (now - timedelta(seconds=5)).isoformat()
                else:
                    refresh_dates(item)
        elif isinstance(value, list):
            for item in value:
                refresh_dates(item)

    refresh_dates(document)
    document["destination_proof"]["target"]["environment"] = "local"
    reconciled_at = (now - timedelta(seconds=2)).isoformat()
    document["destination_proof"]["reconciliation"]["observed_at"] = reconciled_at
    document["destination_proof"]["observed_at"] = (now - timedelta(seconds=1)).isoformat()
    path = tmp_path / "complete.json"
    path.write_text(json.dumps(document))
    adapter = ProjectionRepositoryObservationAdapter(lambda _: f"live:src1:file://{path}")
    evidence = adapter.evidence("ppl1")
    assert evidence.reconciliation.state == "observed"
    assert evidence.reconciliation.observed_at == reconciled_at
    assert evidence.reconciliation.duplicate_count == 0
    assert "Journal window (" in evidence.reconciliation.detail


def test_observe_reads_status_from_the_real_console_document(tmp_path) -> None:
    origin = _write_document(tmp_path, counters={"events_published": 120, "events_in_target": 118})
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    observation = adapter.observe("ppl1")
    assert observation.observed_state is not None  # calculé par project_console_document, pas deviné
    assert observation.rows_source == 120
    assert observation.rows_destination == 118
    assert observation.collected_at is not None


def test_observe_leaves_throughput_and_last_arrival_absent_with_reason(tmp_path) -> None:
    origin = _write_document(tmp_path, counters={"events_published": 1})
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is None
    assert observation.last_arrival_at is None
    assert "throughput" in observation.absent_reasons["throughput_rows_per_second"]
    assert "arrival" in observation.absent_reasons["last_arrival_at"]


def test_observe_uses_configured_counter_names(tmp_path) -> None:
    # ``polls``/``errors`` : deux clés de la liste fermée
    # ``projection.PUBLIC_COUNTERS``, distinctes des défauts
    # ``events_published``/``events_in_target``.
    origin = _write_document(tmp_path, counters={"polls": 7, "errors": 5})
    adapter = ProjectionRepositoryObservationAdapter(
        lambda pipeline_id: f"live:src1:{origin}",
        rows_source_counter="polls",
        rows_destination_counter="errors",
    )
    observation = adapter.observe("ppl1")
    assert observation.rows_source == 7
    assert observation.rows_destination == 5


def test_observe_missing_configured_counter_stays_absent_with_reason(tmp_path) -> None:
    origin = _write_document(tmp_path, counters={"errors": 1})
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    observation = adapter.observe("ppl1")
    assert observation.rows_source is None
    assert "events_published" in observation.absent_reasons["rows_source"]


def test_observe_unreadable_document_returns_absent(tmp_path) -> None:
    origin = f"file://{tmp_path / 'does-not-exist.json'}"
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    observation = adapter.observe("ppl1")
    assert observation.observed_state is None
    assert observation.absent_reasons["observed_state"]


def test_observe_rejects_bad_source_spec_syntax_fail_closed(tmp_path) -> None:
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: "not-a-valid-spec")
    observation = adapter.observe("ppl1")
    assert observation.observed_state is None


def test_metrics_maps_lag_series_buckets_to_points(tmp_path) -> None:
    lag_series = {
        "resolution_s": 5.0,
        "capacity": 240,
        "sample_count": 10,
        "unknown_sample_count": 0,
        "buckets": [
            {"start_s": 0.0, "end_s": 4.0, "min": 8, "max": 12, "last": 9, "samples": 5, "unknown_samples": 0},
            {"start_s": 5.0, "end_s": 9.0, "min": 7, "max": 10, "last": 8, "samples": 5, "unknown_samples": 0},
        ],
    }
    origin = _write_document(tmp_path, counters={"events_published": 1}, lag_series=lag_series)
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    series = adapter.metrics("ppl1", "1h")
    assert series.window == "1h"
    assert len(series.points) == 2
    assert series.points[0].lag_seconds == 9
    assert series.points[1].lag_seconds == 8
    assert series.provenance == "lag_series_projection"
    # le dernier panier est ancré sur observed_at : les points antérieurs
    # doivent être strictement plus anciens.
    assert series.points[0].at < series.points[1].at


def test_metrics_without_lag_series_returns_empty(tmp_path) -> None:
    origin = _write_document(tmp_path, counters={"events_published": 1})
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: f"live:src1:{origin}")
    series = adapter.metrics("ppl1", "24h")
    assert series.points == ()
    assert series.provenance == "absent"


def test_metrics_unmapped_pipeline_returns_empty_with_reason() -> None:
    adapter = ProjectionRepositoryObservationAdapter(lambda pipeline_id: None)
    series = adapter.metrics("ppl-unknown", "1h")
    assert series.points == ()
    assert series.reason == NO_MAPPING_REASON
