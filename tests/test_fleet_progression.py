"""Pilote de progression : la phase ne vient que de mesures, jamais d'un statut."""

from __future__ import annotations

import site_fixture

site_fixture.build_test_site()

import json
import unittest
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

import pytest

from quadringent_control_plane.fleet import (
    MANIFEST,
    FleetError,
    JournalCheckpoint,
    Phase,
    ProofWindow,
    ReceiverChain,
    ReceiverSpan,
    deserialize_fleet,
)
from quadringent_control_plane.fleet_plan import (
    ATTACHED_STATUS,
    CONTINUITY_BROKEN,
    CONTINUITY_PROVEN,
    CONTINUITY_UNCERTAIN,
    CatalogReceiver,
)
from quadringent_control_plane.fleet_progression import (
    RUN_STATE_FORMAT,
    CertificationMeasure,
    FleetEvidence,
    FleetProgression,
    TableMeasure,
    committed_tail,
    continuity_verdict,
    public_phase,
    receiver_chain,
)


START = JournalCheckpoint("DEMOJRN3776", 10)
CURRENT = JournalCheckpoint("DEMOJRN3776", 50)
TAIL = JournalCheckpoint("DEMOJRN3776", 50)
CHAIN = ReceiverChain((ReceiverSpan("DEMOJRN3776", 1, 200),))
INTENT = "intent-a"
RUN_CREATED = "2026-09-20T10:00:00+00:00"


@dataclass
class MemoryStore:
    document: dict[str, object] | None = None
    saved: int = 0

    def load(self) -> dict[str, object] | None:
        return self.document

    def save(self, mapping: Mapping[str, object]) -> None:
        self.document = json.loads(json.dumps(mapping))
        self.saved += 1


@dataclass
class StubPlan:
    max_concurrency: int = 4


def certification(**overrides: object) -> CertificationMeasure:
    """Preuve mesurée complète d'une voie — postérieure au run par défaut."""

    values: dict[str, object] = {
        "window": ProofWindow(
            "2026-09-20T10:00:00+00:00", "2026-09-20T10:05:00+00:00"
        ),
        "source_count": 110,
        "target_count": 110,
        "missing": 0,
        "extra": 0,
        "duplicates": 0,
        "source_hash": "hash-a",
        "target_hash": "hash-a",
        "freshness_seconds": 12.0,
        "latency_seconds": 3.0,
        "throughput_rows_per_second": 0.5,
        "measured_at": "2999-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    return CertificationMeasure(**values)


def measure(
    *,
    table: str = MANIFEST[0],
    estimated: int | None = 100,
    snapshot_rows: int | None = 100,
    published: int | None = 100,
    loaded: int | None = 110,
    certification: CertificationMeasure | None = None,
    estimated_credit: float | None = 0.1,
    measured_credit: float | None = 0.11,
) -> TableMeasure:
    return TableMeasure(
        estimated_rows=estimated,
        snapshot_rows=snapshot_rows,
        snapshot_published=published,
        loaded_rows=loaded,
        certification=certification,
        estimated_credits=None if estimated_credit is None else credit("estimate", estimated_credit),
        measured_credits=None if measured_credit is None else credit("measured", measured_credit, table=table),
    )


def measures(**overrides: object) -> dict[str, TableMeasure]:
    return {name: measure(table=name, **overrides) for name in MANIFEST}


def evidence(
    *,
    history_active: bool = True,
    paused: bool = False,
    start: JournalCheckpoint | None = START,
    current: JournalCheckpoint | None = CURRENT,
    tail: JournalCheckpoint | None = TAIL,
    chain: ReceiverChain | None = CHAIN,
    continuity: bool | None = True,
    gap: bool | None = False,
    tables: Mapping[str, TableMeasure] | None = None,
    intent: str | None = INTENT,
    budget: float | None = 10,
) -> FleetEvidence:
    return FleetEvidence(
        prepare_intent_id=intent,
        start_checkpoint=start,
        history_active=history_active,
        paused=paused,
        current_checkpoint=current,
        committed_tail=tail,
        receiver_chain=chain,
        continuity_proven=continuity,
        gap=gap,
        tables=measures() if tables is None else tables,
        credit_budget=None if budget is None else credit("budget", budget),
    )


def progression(
    store: MemoryStore | None = None,
    provider_evidence: FleetEvidence | None = None,
    *,
    max_concurrency: int = 4,
) -> tuple[FleetProgression, MemoryStore]:
    memory = store or MemoryStore()
    class FixedCreationProgression(FleetProgression):
        def _create(self, evidence):
            run = super()._create(evidence)
            self._created_at = RUN_CREATED
            return run

    driver = FixedCreationProgression(
        plan=StubPlan(max_concurrency=max_concurrency),
        run_store=memory,
        evidence_provider=lambda: provider_evidence,
    )
    return driver, memory


def credit(kind: str, value: float, *, table: str = MANIFEST[0], intent: str = INTENT):
    from quadringent_control_plane import fleet_progression as module

    return module.CreditEvidence(
        credits=value, kind=kind,
        source={"budget": "operator_budget", "estimate": "declared_estimate", "measured": "attributed_metering"}[kind],
        evidence_sha256="a" * 64, observed_at=datetime.now(timezone.utc).isoformat(),
        window=ProofWindow("2026-09-20T10:00:00+00:00", "2026-09-20T10:05:00+00:00") if kind == "measured" else None,
        work_proof=module.CreditWorkProof(intent, table,
            ProofWindow("2026-09-20T10:00:00+00:00", "2026-09-20T10:05:00+00:00"),
            "2026-09-20T10:05:00+00:00", "b" * 64) if kind == "measured" else None,
    )


def financial_state(run, *, intent=INTENT):
    tables = {}
    for table in run.tables:
        entries = {}
        if table.estimated_credits is not None:
            entries["estimate"] = credit("estimate", table.estimated_credits).to_dict()
        if table.actual_credits is not None:
            entries["measured"] = credit("measured", table.actual_credits, table=table.name, intent=intent).to_dict()
        if entries:
            tables[table.name] = entries
    return {"budget": credit("budget", run.credit_budget).to_dict(), "tables": tables}


def test_million_rows_without_a_financial_budget_creates_no_credit_state():
    supplied = evidence(budget=None, tables=measures(estimated=1000000, snapshot_rows=1000000, published=1000000, loaded=1000000))
    driver, memory = progression(provider_evidence=supplied)
    outcome = driver.tick()
    assert outcome.reason == "unknown_credit_budget"
    assert memory.document is None


def test_explicit_credits_are_independent_of_million_row_counters():
    from dataclasses import replace

    supplied = replace(evidence(tables={name: TableMeasure(
        estimated_rows=1000000, snapshot_rows=1000000, snapshot_published=1000000, loaded_rows=1000000,
        estimated_credits=credit("estimate", 0.1), measured_credits=credit("measured", 0.11, table=name),
    ) for name in MANIFEST}), credit_budget=credit("budget", 10))
    driver, memory = progression(provider_evidence=supplied)
    for _ in range(8):
        outcome = driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    assert outcome.phase == "LIVE" and run.credit_budget == 10
    assert all(t.copied_rows == t.total_rows == 1000000 and t.actual_credits == 0.11 for t in run.tables)
    assert all(t.estimated_credits == 0.1 for t in run.tables)
    stored = memory.document["credit_evidence"]
    assert stored["budget"]["source"] == "operator_budget"
    assert stored["tables"][MANIFEST[0]]["measured"]["evidence_sha256"] == "a" * 64


def test_loaded_rows_do_not_supply_a_missing_measurement():
    from dataclasses import replace

    supplied = replace(evidence(tables={name: TableMeasure(
        estimated_rows=1000000, snapshot_rows=1000000, snapshot_published=1000000, loaded_rows=1000000,
        estimated_credits=credit("estimate", 0.1),
    ) for name in MANIFEST}), credit_budget=credit("budget", 10))
    driver, memory = progression(provider_evidence=supplied)
    for _ in range(4):
        outcome = driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    active = [t for t in run.tables if t.admitted]
    assert active and all(t.phase is Phase.CATCHING_UP and t.actual_credits is None for t in active)
    assert any("unknown_cost" in error for error in outcome.errors)


def test_explicit_budget_does_not_replace_an_unknown_estimate():
    from dataclasses import replace

    driver, memory = progression(provider_evidence=replace(
        evidence(tables={name: TableMeasure(estimated_rows=1000000) for name in MANIFEST}), credit_budget=credit("budget", 10)))
    outcome = driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    assert all(t.phase is Phase.READY and t.estimated_credits is None for t in run.tables)
    assert any("unknown_cost" in error for error in outcome.errors)


@pytest.mark.parametrize("kind", ["budget", "estimate", "measured"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, True, "0.1", 10**1000])
def test_invalid_credit_values_are_not_measurements(kind, value):
    with pytest.raises(FleetError):
        credit(kind, value)


def test_zero_is_an_explicit_measurement_not_missing_evidence():
    from dataclasses import replace

    supplied = replace(evidence(tables={name: TableMeasure(
        estimated_rows=100, snapshot_rows=100, snapshot_published=100, loaded_rows=110,
        estimated_credits=credit("estimate", 0.1), measured_credits=credit("measured", 0, table=name),
    ) for name in MANIFEST}), credit_budget=credit("budget", 10))
    driver, memory = progression(provider_evidence=supplied)
    for _ in range(8):
        outcome = driver.tick()
    assert outcome.phase == "LIVE"
    assert all(t.actual_credits == 0 for t in deserialize_fleet(memory.document["fleet"]).tables)


def test_legacy_row_based_credit_state_is_quarantined_without_overwrite():
    from quadringent_control_plane import fleet as domain

    run = domain.create_fleet(max_concurrency=1, credit_budget=4000000)
    for name in MANIFEST:
        run = domain.prepare_table(run, name, START)
    run = domain.admit_next(run, estimated_credits=1000000)
    run = domain.record_history_progress(run, MANIFEST[0], copied_rows=1000000, total_rows=1000000)
    run = domain.record_actual_cost(run, MANIFEST[0], 1000000)
    original = {"format_version": "quadringent-fleet-run-state-v1", "environment": domain.ENVIRONMENT,
                "prepare_intent_id": INTENT, "created_at": "2026-09-20T10:00:00+00:00", "fleet": domain.serialize_fleet(run)}
    memory = MemoryStore(document=deepcopy(original))
    driver, _ = progression(memory, provider_evidence=evidence())
    outcome = driver.tick()
    assert outcome.status == "halted" and outcome.reason == "unverified_credit_state"
    assert outcome.phase is None and memory.document == original and memory.saved == 0


def test_explicit_estimate_cannot_exceed_the_declared_budget():
    supplied = evidence(budget=0.05)
    driver, memory = progression(provider_evidence=supplied)
    outcome = driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    assert all(t.phase is Phase.READY and t.actual_credits is None for t in run.tables)
    assert any("admission_refused" in error for error in outcome.errors)


def test_measured_overrun_keeps_the_domain_financial_guard():
    driver, memory = progression(provider_evidence=evidence(
        budget=0.15, tables=measures(measured_credit=0.2)), max_concurrency=1)
    driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    assert run.tables[0].phase is Phase.BLOCKED
    assert run.tables[0].blocked_reason == "cost_overrun"
    assert run.tables[0].actual_credits == 0.2 and run.consumed_credits > run.credit_budget
    assert run.tables[0].copied_rows == run.tables[0].total_rows == 100


def test_credit_provenance_is_checked_again_when_loading_persisted_state():
    driver, memory = progression(provider_evidence=evidence())
    driver.tick()
    memory.document["credit_evidence"]["tables"][MANIFEST[0]]["measured"]["credits"] = 1000000
    original = deepcopy(memory.document)
    saved = memory.saved
    with pytest.raises(FleetError, match="differ"):
        driver.tick()
    assert memory.document == original and memory.saved == saved


def test_estimate_cannot_be_supplied_as_a_measurement():
    with pytest.raises(FleetError):
        TableMeasure(measured_credits=credit("estimate", 0.2))


def test_changed_budget_cannot_silently_reuse_a_persisted_limit():
    driver, memory = progression(provider_evidence=evidence())
    driver.tick()
    original, saved = deepcopy(memory.document), memory.saved
    restarted, _ = progression(memory, provider_evidence=evidence(budget=0.05))
    outcome = restarted.tick()
    assert outcome.status == "halted" and outcome.reason == "credit_budget_changed"
    assert memory.document == original and memory.saved == saved


def test_pre_run_metering_never_supplies_current_table_cost():
    from dataclasses import replace
    from quadringent_control_plane.fleet_progression import CreditEvidence

    stale = CreditEvidence(0, "measured", "attributed_metering", "b" * 64,
                           "2000-01-02T00:00:00Z", ProofWindow("2000-01-01T00:00:00Z", "2000-01-01T00:01:00Z"))
    supplied = evidence(tables={name: replace(measure(), measured_credits=stale) for name in MANIFEST})
    driver, memory = progression(provider_evidence=supplied)
    for _ in range(8):
        outcome = driver.tick()
    run = deserialize_fleet(memory.document["fleet"])
    assert all(table.actual_credits is None for table in run.tables)
    assert outcome.phase != "LIVE"
    assert all("measured" not in value for value in memory.document["credit_evidence"]["tables"].values())


class ProgressionTest(unittest.TestCase):
    def test_no_evidence_stays_idle(self) -> None:
        driver, _memory = progression(provider_evidence=None)
        outcome = driver.tick()
        self.assertEqual(outcome.status, "idle")
        self.assertEqual(outcome.reason, "evidence_unavailable")

    def test_history_not_started_creates_nothing(self) -> None:
        driver, memory = progression(provider_evidence=evidence(history_active=False))
        outcome = driver.tick()
        self.assertEqual(outcome.status, "idle")
        self.assertEqual(outcome.reason, "history_not_started")
        self.assertIsNone(memory.document)

    def test_create_then_advance_all_tables_to_live(self) -> None:
        driver, memory = progression(provider_evidence=evidence())
        # Quatre slots d'admission : la flotte se vide par passes bornées,
        # chaque transition restant conditionnée aux mesures du cycle.
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "LIVE":
                break
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, "advanced")
        self.assertEqual(outcome.errors, ())
        self.assertEqual(outcome.phase, "LIVE")
        self.assertEqual(
            set(outcome.table_phases.values()), {Phase.LIVE.value}
        )
        # Le run est persisté dans le document durable.
        self.assertIsNotNone(memory.document)
        self.assertEqual(memory.document["format_version"], RUN_STATE_FORMAT)
        self.assertEqual(memory.document["prepare_intent_id"], INTENT)
        run = deserialize_fleet(memory.document["fleet"])
        self.assertTrue(all(t.phase is Phase.LIVE for t in run.tables))
        for table in run.tables:
            self.assertEqual(table.copied_rows, 100)
            self.assertEqual(table.total_rows, 100)
            self.assertEqual(table.actual_credits, 0.11)

    def test_concurrency_bounds_admission(self) -> None:
        # Quatre slots : deux tables encore prêtes attendent leur tour tant
        # que les autres copient.
        driver, memory = progression(provider_evidence=None)
        first = evidence(
            tail=JournalCheckpoint("DEMOJRN3776", 400),
            current=JournalCheckpoint("DEMOJRN3776", 60),
            tables={
                name: measure(
                    table=name,
                    snapshot_rows=50, published=100
                )
                for name in MANIFEST
            },
        )
        driver2 = FleetProgression(
            plan=StubPlan(max_concurrency=2),
            run_store=memory,
            evidence_provider=lambda: first,
        )
        outcome = driver2.tick()
        admitted = [
            phase
            for phase in outcome.table_phases.values()
            if phase != Phase.READY.value
        ]
        self.assertEqual(len(admitted), 2)

    def test_incomplete_history_stays_historical(self) -> None:
        pending = evidence(
            tables={name: measure(snapshot_rows=40, published=100) for name in MANIFEST},
        )
        driver, memory = progression(provider_evidence=pending)
        outcome = driver.tick()
        self.assertEqual(outcome.phase, "HISTORICAL")
        run = deserialize_fleet(memory.document["fleet"])
        admitted = [t for t in run.tables if t.phase is not Phase.READY]
        self.assertTrue(all(t.phase is Phase.HISTORICAL for t in admitted))
        self.assertTrue(all(t.copied_rows == 40 for t in admitted))

    def test_unknown_published_total_keeps_history_open(self) -> None:
        pending = evidence(
            tables={name: measure(snapshot_rows=100, published=None) for name in MANIFEST},
        )
        driver, _memory = progression(provider_evidence=pending)
        outcome = driver.tick()
        self.assertEqual(outcome.phase, "HISTORICAL")

    def test_lagging_cursor_stays_catching_up(self) -> None:
        # Un curseur en retard garde les voies admises en CATCHING_UP : elles
        # occupent leurs slots, aucune ne franchit LIVE tant que le tail
        # n'est pas atteint.
        behind = evidence(
            current=JournalCheckpoint("DEMOJRN3776", 30),
            tail=JournalCheckpoint("DEMOJRN3776", 80),
        )
        driver, memory = progression(provider_evidence=behind)
        for _ in range(4):
            outcome = driver.tick()
        run = deserialize_fleet(memory.document["fleet"])
        admitted = [t for t in run.tables if t.phase is not Phase.READY]
        self.assertEqual(len(admitted), 4)
        self.assertTrue(all(t.phase is Phase.CATCHING_UP for t in admitted))
        self.assertEqual(outcome.phase, "HISTORICAL")

    def test_unknown_cost_stays_catching_up(self) -> None:
        unpriced = evidence(
            tables={name: measure(loaded=None, measured_credit=None) for name in MANIFEST},
        )
        driver, memory = progression(provider_evidence=unpriced)
        for _ in range(4):
            driver.tick()
        run = deserialize_fleet(memory.document["fleet"])
        admitted = [t for t in run.tables if t.phase is not Phase.READY]
        self.assertTrue(all(t.phase is Phase.CATCHING_UP for t in admitted))

    def test_broken_continuity_blocks(self) -> None:
        broken = evidence(continuity=False, gap=True)
        driver, _memory = progression(provider_evidence=broken)
        outcome = driver.tick()
        self.assertEqual(outcome.phase, "BLOCKED")
        # Les tables admises sont bloquées par la continuité cassée ; les
        # slots libérés admettent les suivantes — le treizième se résorbe au
        # cycle suivant, jamais bloqué à sa place.
        blocked = sum(
            1 for phase in outcome.table_phases.values() if phase == "BLOCKED"
        )
        self.assertGreaterEqual(blocked, 4)
        outcome = driver.tick()
        self.assertEqual(
            set(outcome.table_phases.values()), {Phase.BLOCKED.value}
        )

    def test_start_outside_chain_blocks_receiver_discontinuity(self) -> None:
        orphan = evidence(
            start=JournalCheckpoint("DEMOJRN0001", 5),
            chain=ReceiverChain((ReceiverSpan("DEMOJRN3776", 1, 200),)),
        )
        driver, _memory = progression(provider_evidence=orphan)
        outcome = driver.tick()
        self.assertEqual(outcome.phase, "BLOCKED")

    def test_uncertain_continuity_waits_without_blocking(self) -> None:
        uncertain = evidence(continuity=None, gap=None)
        driver, _memory = progression(provider_evidence=uncertain)
        outcome = driver.tick()
        self.assertEqual(outcome.phase, "HISTORICAL")
        self.assertEqual(outcome.errors, ())

    def test_paused_freezes_progression(self) -> None:
        driver, memory = progression(provider_evidence=evidence(paused=True))
        outcome = driver.tick()
        self.assertEqual(outcome.status, "paused")
        self.assertIsNotNone(memory.document)

    def test_restart_resumes_persisted_run(self) -> None:
        driver, memory = progression(provider_evidence=evidence())
        first = None
        for _ in range(8):
            first = driver.tick()
            if first.phase == "LIVE":
                break
        self.assertIsNotNone(first)
        self.assertEqual(first.phase, "LIVE")
        # Un second pilote sur le même store reprend sans re-créer.
        second_driver, _ = progression(memory, provider_evidence=evidence())
        second = second_driver.tick()
        self.assertEqual(second.phase, "LIVE")
        self.assertEqual(second.status, "idle")

    def test_stale_prepare_intent_halts_without_overwrite(self) -> None:
        driver, memory = progression(provider_evidence=evidence())
        driver.tick()
        new_driver, _ = progression(
            memory, provider_evidence=evidence(intent="intent-b")
        )
        outcome = new_driver.tick()
        self.assertEqual(outcome.status, "halted")
        self.assertEqual(outcome.reason, "stale_prepare_intent")
        self.assertEqual(memory.document["prepare_intent_id"], INTENT)

    def test_corrupt_run_state_raises(self) -> None:
        memory = MemoryStore(document={"format_version": "other"})
        driver, _ = progression(memory, provider_evidence=evidence())
        with self.assertRaises(Exception):
            driver.tick()

    def test_live_tables_certify_on_measured_proof(self) -> None:
        certified = evidence(
            tables={
                name: measure(table=name, certification=certification()) for name in MANIFEST
            }
        )
        driver, memory = progression(provider_evidence=certified)
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "CERTIFIED":
                break
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.phase, "CERTIFIED")
        self.assertEqual(outcome.errors, ())
        run = deserialize_fleet(memory.document["fleet"])
        self.assertTrue(all(t.phase is Phase.CERTIFIED for t in run.tables))
        for table in run.tables:
            self.assertIsNotNone(table.reconciliation_proof)
            self.assertEqual(table.reconciliation_proof.source_hash, "hash-a")

    def test_certification_waits_for_proof(self) -> None:
        driver, memory = progression(provider_evidence=evidence())
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "LIVE":
                break
        self.assertEqual(outcome.phase, "LIVE")
        # Une voie LIVE sans preuve mesurée reste LIVE — jamais certifiée
        # sur un statut.
        run = deserialize_fleet(memory.document["fleet"])
        self.assertTrue(all(t.phase is Phase.LIVE for t in run.tables))

    def test_pre_run_proof_is_not_admitted(self) -> None:
        stale = evidence(
            tables={
                name: measure(
                    table=name,
                    certification=certification(
                        measured_at="2020-01-01T00:00:00+00:00"
                    )
                )
                for name in MANIFEST
            }
        )
        driver, memory = progression(provider_evidence=stale)
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "LIVE":
                break
        # La preuve précède la création du run : elle appartient à une
        # génération passée — les voies plafonnent à LIVE.
        self.assertEqual(outcome.phase, "LIVE")
        run = deserialize_fleet(memory.document["fleet"])
        self.assertTrue(all(t.phase is Phase.LIVE for t in run.tables))

    def test_divergent_proof_blocks_and_recovers(self) -> None:
        divergent = evidence(
            tables={
                name: measure(
                    table=name,
                    certification=certification(target_hash="hash-b")
                )
                for name in MANIFEST
            }
        )
        driver, memory = progression(provider_evidence=divergent)
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "RECONCILING":
                break
        self.assertEqual(outcome.phase, "RECONCILING")
        self.assertTrue(
            any("hash_mismatch" in error for error in outcome.errors),
            outcome.errors,
        )
        # La divergence reste déclarée tant qu'elle dure.
        outcome = driver.tick()
        self.assertTrue(
            any("hash_mismatch" in error for error in outcome.errors),
            outcome.errors,
        )
        run = deserialize_fleet(memory.document["fleet"])
        admitted = [t for t in run.tables if t.phase is not Phase.READY]
        self.assertTrue(all(t.phase is Phase.RECONCILING for t in admitted))
        # La sonde publie une mesure corrigée sur une fenêtre nouvelle :
        # un pilote frais sur le même store ré-ancre puis certifie dans le
        # même relevé — la reprise après redémarrage est le chemin réel.
        corrected = evidence(
            tables={
                name: measure(
                    table=name,
                    certification=certification(
                        window=ProofWindow(
                            "2026-09-20T11:00:00+00:00",
                            "2026-09-20T11:05:00+00:00",
                        )
                    )
                )
                for name in MANIFEST
            }
        )
        recovered, _ = progression(memory, provider_evidence=corrected)
        outcome = None
        for _ in range(8):
            outcome = recovered.tick()
            if outcome.phase == "CERTIFIED":
                break
        self.assertEqual(outcome.phase, "CERTIFIED")
        self.assertEqual(outcome.errors, ())

    def test_freshness_breach_declared_and_kept(self) -> None:
        breached = evidence(
            tables={
                name: measure(
                    table=name,
                    certification=certification(freshness_seconds=301.0)
                )
                for name in MANIFEST
            }
        )
        driver, memory = progression(provider_evidence=breached)
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "RECONCILING":
                break
        self.assertEqual(outcome.phase, "RECONCILING")
        self.assertTrue(
            any("freshness_slo_breached" in error for error in outcome.errors),
            outcome.errors,
        )
        run = deserialize_fleet(memory.document["fleet"])
        admitted = [t for t in run.tables if t.phase is not Phase.READY]
        self.assertTrue(all(t.phase is Phase.RECONCILING for t in admitted))


class HelpersTest(unittest.TestCase):
    def _receiver(
        self, name: str, status: str, first: int, last: int
    ) -> CatalogReceiver:
        return CatalogReceiver(
            library="JRNLIB",
            name=name,
            status=status,
            first_sequence=first,
            last_sequence=last,
            attach_timestamp="2026-09-18T08:00:00Z",
            detach_timestamp=None,
            previous_library=None,
            previous_name=None,
        )

    def test_committed_tail_attached_is_last_minus_one(self) -> None:
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", ATTACHED_STATUS, 101, 250),
        )
        tail = committed_tail(receivers)
        self.assertEqual(tail, JournalCheckpoint("DEMOJRN2", 249))

    def test_committed_tail_detached_is_final_last(self) -> None:
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", "ONLINE", 101, 250),
        )
        tail = committed_tail(receivers)
        self.assertEqual(tail, JournalCheckpoint("DEMOJRN2", 250))

    def test_committed_tail_fresh_attached_falls_back_to_previous(self) -> None:
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", ATTACHED_STATUS, 101, 101),
        )
        tail = committed_tail(receivers)
        self.assertEqual(tail, JournalCheckpoint("DEMOJRN1", 100))

    def test_committed_tail_single_fresh_receiver_is_none(self) -> None:
        receivers = (self._receiver("DEMOJRN1", ATTACHED_STATUS, 5, 5),)
        self.assertIsNone(committed_tail(receivers))

    def test_receiver_chain_uses_catalog_bounds(self) -> None:
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", ATTACHED_STATUS, 101, 250),
        )
        chain = receiver_chain(receivers)
        self.assertIsNotNone(chain)
        self.assertEqual(chain.spans[0].receiver, "DEMOJRN1")
        self.assertEqual(chain.spans[1].last_sequence, 250)

    def test_attached_span_is_open_and_ranks_beyond_the_snapshot(self) -> None:
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", ATTACHED_STATUS, 101, 250),
        )
        chain = receiver_chain(receivers)
        self.assertFalse(chain.spans[0].open)
        self.assertTrue(chain.spans[1].open)
        # La borne cataloguée est un instantané : le lecteur peut avoir
        # déjà commis au-delà sans que ce soit une discontinuité.
        self.assertEqual(chain.rank(JournalCheckpoint("DEMOJRN2", 999)), (1, 999))

    def test_open_span_is_only_allowed_last(self) -> None:
        with self.assertRaises(FleetError):
            ReceiverChain(
                (
                    ReceiverSpan("DEMOJRN1", 10, 100, open=True),
                    ReceiverSpan("DEMOJRN2", 101, 250),
                )
            )

    def test_closed_chain_still_rejects_current_beyond_tail(self) -> None:
        # Chaîne close (journal détaché) : une position au-delà de la queue
        # reste une contradiction réelle, pas de la fraîcheur.
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", "ONLINE", 101, 250),
        )
        chain = receiver_chain(receivers)
        self.assertFalse(any(span.open for span in chain.spans))
        closed = evidence(
            start=JournalCheckpoint("DEMOJRN1", 10),
            current=JournalCheckpoint("DEMOJRN2", 250),
            tail=JournalCheckpoint("DEMOJRN2", 249),
            chain=chain,
        )
        driver, _memory = progression(provider_evidence=closed)
        outcome = driver.tick()
        self.assertTrue(
            any("invalid_journal_evidence" in error for error in outcome.errors),
            outcome.errors,
        )

    def test_reader_ahead_of_attached_tail_never_blocks(self) -> None:
        # Le cas INT réel : le lecteur a commis au-delà du last_sequence
        # catalogué du receiver attaché — staleness, pas discontinuité.
        receivers = (
            self._receiver("DEMOJRN1", "ONLINE", 10, 100),
            self._receiver("DEMOJRN2", ATTACHED_STATUS, 101, 250),
        )
        chain = receiver_chain(receivers)
        fresh = evidence(
            start=JournalCheckpoint("DEMOJRN1", 10),
            current=JournalCheckpoint("DEMOJRN2", 400),
            tail=JournalCheckpoint("DEMOJRN2", 249),
            chain=chain,
        )
        driver, memory = progression(provider_evidence=fresh)
        outcome = None
        for _ in range(8):
            outcome = driver.tick()
            if outcome.phase == "LIVE":
                break
        self.assertFalse(
            any("discontinuity" in error for error in outcome.errors),
            outcome.errors,
        )
        # En avance sur la queue observée = rattrapé : la voie passe LIVE,
        # l'égalité stricte coincerait la flotte en CATCHING_UP à jamais.
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.phase, "LIVE")
        run = deserialize_fleet(memory.document["fleet"])
        self.assertTrue(all(t.phase is Phase.LIVE for t in run.tables))

    def test_continuity_verdict_mapping(self) -> None:
        self.assertEqual(continuity_verdict(CONTINUITY_PROVEN), (True, False))
        self.assertEqual(continuity_verdict(CONTINUITY_BROKEN), (False, True))
        self.assertEqual(continuity_verdict(CONTINUITY_UNCERTAIN), (None, None))
        self.assertEqual(continuity_verdict(None), (None, None))


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("change", ["intent", "table", "late_start", "short_window", "proof_before_run", "observed_before_proof", "no_binding"])
def test_metering_requires_causal_current_run_table_binding(change):
    from dataclasses import replace

    tables = {}
    for name in MANIFEST:
        measured = credit("measured", 0, table=name)
        proof = measured.work_proof
        if change == "intent":
            measured = replace(measured, work_proof=replace(proof, prepare_intent_id="old-intent"))
        elif change == "table":
            other = next(table for table in MANIFEST if table != name)
            measured = replace(measured, work_proof=replace(proof, table_name=other))
        elif change == "late_start":
            measured = replace(measured, window=ProofWindow("2026-09-20T10:00:01Z", "2026-09-20T10:05:00Z"))
        elif change == "short_window":
            measured = replace(measured, window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-20T10:04:59Z"))
        elif change == "proof_before_run":
            measured = replace(measured, work_proof=replace(proof,
                window=ProofWindow("2000-01-01T00:00:00Z", "2000-01-01T00:01:00Z"),
                measured_at="2000-01-01T00:01:00Z"))
        elif change == "observed_before_proof":
            measured = replace(measured, window=ProofWindow("2000-01-01T00:00:00Z", "2000-01-01T00:01:00Z"),
                               observed_at="2000-01-02T00:00:00Z")
        elif change == "no_binding":
            measured = replace(measured, work_proof=None)
        tables[name] = replace(measure(table=name), measured_credits=measured)
    driver, memory = progression(provider_evidence=evidence(tables=tables))
    for _ in range(4):
        outcome = driver.tick()
    assert outcome.phase != "LIVE"
    assert all(table.actual_credits is None for table in deserialize_fleet(memory.document["fleet"]).tables)
    assert any("unknown_cost" in error for error in outcome.errors)


def test_larger_completed_billing_window_and_long_metering_lag_are_admissible():
    from dataclasses import replace

    tables = {name: replace(measure(table=name), measured_credits=replace(credit("measured", 0.11, table=name),
        window=ProofWindow("2026-09-19T00:00:00Z", "2026-09-21T00:00:00Z"))) for name in MANIFEST}
    driver, memory = progression(provider_evidence=evidence(tables=tables))
    for _ in range(8):
        outcome = driver.tick()
    assert outcome.phase == "LIVE"
    measured = memory.document["credit_evidence"]["tables"][MANIFEST[0]]["measured"]
    assert measured["evidence_sha256"] == "a" * 64
    assert measured["work_proof"]["canonical_sha256"] == "b" * 64
    assert measured["work_proof"]["prepare_intent_id"] == INTENT


@pytest.mark.parametrize("change", ["decreased", "moving_start", "shrinking_end", "older_observation", "older_proof"])
def test_cumulative_metering_cannot_regress_or_switch_windows_before_live(change):
    from dataclasses import replace

    initial = evidence(tail=JournalCheckpoint(TAIL.receiver, 60), tables={
        name: replace(measure(table=name), measured_credits=replace(credit("measured", 0.11, table=name),
            window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-21T00:00:00Z"))) for name in MANIFEST})
    driver, memory = progression(provider_evidence=initial)
    driver.tick()
    previous = deepcopy(memory.document["credit_evidence"]["tables"][MANIFEST[0]]["measured"])
    incoming = {}
    for name, table in initial.tables.items():
        measured = replace(table.measured_credits, credits=0.12)
        if change == "decreased":
            measured = replace(measured, credits=0.01)
        elif change == "moving_start":
            measured = replace(measured, window=ProofWindow("2026-09-19T00:00:00Z", "2026-09-21T00:00:00Z"))
        elif change == "shrinking_end":
            measured = replace(measured, window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z"))
        elif change == "older_observation":
            measured = replace(measured, observed_at="2026-09-21T01:00:00Z")
        elif change == "older_proof":
            measured = replace(measured, work_proof=replace(measured.work_proof,
                window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-20T10:04:00Z"),
                measured_at="2026-09-20T10:04:00Z"))
        incoming[name] = replace(table, measured_credits=measured)
    driver._evidence_provider = lambda: replace(initial, tables=incoming)
    outcome = driver.tick()
    assert memory.document["credit_evidence"]["tables"][MANIFEST[0]]["measured"] == previous
    assert deserialize_fleet(memory.document["fleet"]).tables[0].actual_credits == 0.11
    assert any("contradictory_cost" in error for error in outcome.errors)


def test_unchanged_credit_amount_keeps_new_current_proof_and_original_metering_hash():
    from dataclasses import replace

    initial = evidence(tail=JournalCheckpoint(TAIL.receiver, 60))
    driver, memory = progression(provider_evidence=initial)
    driver.tick()
    incoming = {name: replace(table, measured_credits=replace(table.measured_credits, evidence_sha256="c" * 64,
        window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-20T10:07:00Z"),
        work_proof=replace(table.measured_credits.work_proof,
            window=ProofWindow("2026-09-20T10:00:00Z", "2026-09-20T10:06:00Z"),
            measured_at="2026-09-20T10:06:00Z", canonical_sha256="d" * 64))) for name, table in initial.tables.items()}
    driver._evidence_provider = lambda: replace(initial, tables=incoming)
    driver.tick()
    measured = memory.document["credit_evidence"]["tables"][MANIFEST[0]]["measured"]
    assert measured["credits"] == 0.11 and measured["evidence_sha256"] == "c" * 64
    assert measured["work_proof"]["canonical_sha256"] == "d" * 64


def test_pre_binding_v2_measurement_cannot_be_reused_or_rewritten():
    driver, memory = progression(provider_evidence=evidence())
    driver.tick()
    for table in memory.document["credit_evidence"]["tables"].values():
        if "measured" in table:
            table["measured"].pop("work_proof", None)
    original, saved = deepcopy(memory.document), memory.saved
    with pytest.raises(FleetError, match="current run/table"):
        driver.tick()
    assert memory.document == original and memory.saved == saved
