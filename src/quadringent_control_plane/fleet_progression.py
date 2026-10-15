"""Pilote de progression de flotte : des relevés mesurés vers l'état domaine.

Le domaine (:mod:`fleet`) connaît les phases et leurs gardes — il ne devine
rien. Ce module est la seule autorité qui l'alimente : à chaque cycle il lit
les mesures déjà produites par les sondes (copie snapshot chargée, borne
publiée, curseur et queue du journal, chaîne de receivers cataloguée), les
rejoue contre le ``FleetRun`` durable, puis persiste ``fleet-run.json`` — la
projection le reprend tel quel, sans recalcul.

Aucun statut ne fait avancer une table : seuls les compteurs mesurés le
font. Une mesure absente laisse la phase où elle est ; une contradiction
domaine (``FleetError``) gèle la table concernée et est déclarée dans le
résultat — jamais absorbée en silence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any

from . import fleet as _fleet
from .fleet import (
    FleetError,
    FleetRun,
    JournalCheckpoint,
    Phase,
    ProofWindow,
    ReceiverChain,
    ReceiverSpan,
    ReconciliationProof,
    TableState,
)
from .fleet_plan import (
    ATTACHED_STATUS,
    CONTINUITY_BROKEN,
    CONTINUITY_PROVEN,
    CatalogReceiver,
)


RUN_STATE_FORMAT = "quadringent-fleet-run-state-v2"
RUN_STATE_FILE = "fleet-run.json"
CREDIT_EVIDENCE_FORMAT = "quadringent-fleet-credit-evidence-v1"
# Passes bornées par cycle : une table peut franchir HISTORICAL→CATCHING_UP
# →LIVE dans le même relevé quand toutes les mesures sont déjà là — trois
# suffisent, une quatrième ne servirait qu'à masquer une boucle.
_MAX_PASSES = 3
# SLO de fraîcheur destination déclaré par le site (slo-policy : seuil
# ``destination_freshness_seconds``). La certification le rejoue tel quel.
CERTIFICATION_FRESHNESS_SLO_SECONDS = 300.0

_PHASE_RANK: dict[Phase, int] = {
    Phase.NOT_PREPARED: 0,
    Phase.READY: 1,
    Phase.HISTORICAL: 2,
    Phase.CATCHING_UP: 3,
    Phase.LIVE: 4,
    Phase.RECONCILING: 5,
    Phase.CERTIFIED: 6,
    Phase.PAUSED: 7,
    Phase.BLOCKED: 8,
}
_RANK_PUBLIC = (
    "NOT_PREPARED",
    "PREPARED",
    "HISTORICAL",
    "CATCHING_UP",
    "LIVE",
    "RECONCILING",
    "CERTIFIED",
)
_JOB_PHASE_RANK = {"NOT_PREPARED": 0, "PREPARED": 1, "HISTORICAL": 2}


@dataclass(frozen=True)
class CertificationMeasure:
    """Relevé de certification d'une voie, figé à ``window.end_utc``.

    Chaque champ est la mesure publiée dans ``console-proof.json`` sous
    ``certify.tables`` par la sonde — le pilote n'en invente aucun : la
    fenêtre, les comptes, les empreintes et les durées sont recopiés tels
    quels dans le ``ReconciliationProof`` du domaine.
    """

    window: ProofWindow
    source_count: int
    target_count: int
    missing: int
    extra: int
    duplicates: int
    source_hash: str
    target_hash: str
    freshness_seconds: float
    latency_seconds: float
    throughput_rows_per_second: float
    measured_at: str


@dataclass(frozen=True)
class CreditWorkProof:
    """Canonical table proof binding, separate from the original metering hash."""

    prepare_intent_id: str
    table_name: str
    window: ProofWindow
    measured_at: str
    canonical_sha256: str

    def __post_init__(self) -> None:
        measured = _parse_utc_instant(self.measured_at)
        if (type(self.prepare_intent_id) is not str or not self.prepare_intent_id.strip()
            or self.table_name not in _fleet.MANIFEST or type(self.window) is not ProofWindow
            or measured is None or measured > datetime.now(timezone.utc)
            or _parse_utc_instant(self.window.end_utc) > measured
            or type(self.canonical_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", self.canonical_sha256) is None):
            raise FleetError("invalid_credit_evidence", "Current table work proof is required")
        object.__setattr__(self, "window", ProofWindow(
            _parse_utc_instant(self.window.start_utc).isoformat(), _parse_utc_instant(self.window.end_utc).isoformat()))
        object.__setattr__(self, "measured_at", measured.isoformat())

    def to_dict(self) -> dict[str, object]:
        return {"prepare_intent_id": self.prepare_intent_id, "table_name": self.table_name,
                "window": self.window.to_dict(), "measured_at": self.measured_at,
                "canonical_sha256": self.canonical_sha256}

    @classmethod
    def from_dict(cls, value: object) -> CreditWorkProof:
        keys = {"prepare_intent_id", "table_name", "window", "measured_at", "canonical_sha256"}
        if (not isinstance(value, Mapping) or set(value) != keys or not isinstance(value["window"], Mapping)
            or set(value["window"]) != {"start_utc", "end_utc"}):
            raise FleetError("invalid_credit_evidence", "Missing canonical table work proof")
        return cls(value["prepare_intent_id"], value["table_name"], ProofWindow(**value["window"]),
                   value["measured_at"], value["canonical_sha256"])


@dataclass(frozen=True)
class CreditEvidence:
    """Explicit credit units and their evidence reference, never row counts."""

    credits: float
    kind: str
    source: str
    evidence_sha256: str
    observed_at: str
    window: ProofWindow | None = None
    work_proof: CreditWorkProof | None = None

    def __post_init__(self) -> None:
        sources = {"budget": "operator_budget", "estimate": "declared_estimate", "measured": "attributed_metering"}
        observed = _parse_utc_instant(self.observed_at)
        try:
            number = float(self.credits) if type(self.credits) in (int, float) else float("nan")
        except OverflowError:
            number = float("nan")
        if (type(self.kind) is not str or self.kind not in sources or self.source != sources[self.kind]
            or not math.isfinite(number) or number < 0 or (self.kind == "estimate" and number == 0)
            or type(self.evidence_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", self.evidence_sha256) is None
            or observed is None or observed > datetime.now(timezone.utc)):
            raise FleetError("invalid_credit_evidence", "Explicit finite credits and provenance are required")
        if self.kind == "measured":
            if type(self.window) is not ProofWindow or _parse_utc_instant(self.window.end_utc) > observed:
                raise FleetError("invalid_credit_evidence", "Measured credits require a completed evidence window")
            if self.work_proof is not None and type(self.work_proof) is not CreditWorkProof:
                raise FleetError("invalid_credit_evidence", "Invalid table work proof")
        elif self.window is not None:
            raise FleetError("invalid_credit_evidence", "Only measured credits carry a metering window")
        if self.kind != "measured" and self.work_proof is not None:
            raise FleetError("invalid_credit_evidence", "Declarations are distinct from measured table work")
        object.__setattr__(self, "credits", number)

    def to_dict(self) -> dict[str, object]:
        result = {"credits": self.credits, "kind": self.kind, "source": self.source,
                  "evidence_sha256": self.evidence_sha256, "observed_at": self.observed_at,
                  "window": None if self.window is None else self.window.to_dict()}
        if self.kind == "measured":
            result["work_proof"] = None if self.work_proof is None else self.work_proof.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object, kind: str) -> CreditEvidence:
        keys = {"credits", "kind", "source", "evidence_sha256", "observed_at", "window"}
        if isinstance(value, Mapping) and kind == "measured" and "work_proof" in value:
            keys.add("work_proof")
        if not isinstance(value, Mapping) or set(value) != keys or value["kind"] != kind:
            raise FleetError("invalid_credit_evidence", "Credit evidence differs from its declared purpose")
        window = value["window"]
        if window is not None:
            if not isinstance(window, Mapping) or set(window) != {"start_utc", "end_utc"}:
                raise FleetError("invalid_credit_evidence", "Invalid credit observation window")
            window = ProofWindow(window["start_utc"], window["end_utc"])
        work = value.get("work_proof")
        return cls(value["credits"], value["kind"], value["source"], value["evidence_sha256"], value["observed_at"], window,
                   None if work is None else CreditWorkProof.from_dict(work))


def _measurement_matches_run(observation: CreditEvidence, intent: str, table: str, created_at: str) -> bool:
    proof = observation.work_proof
    created = _parse_utc_instant(created_at)
    if proof is None or created is None or proof.prepare_intent_id != intent or proof.table_name != table:
        return False
    start, end = (_parse_utc_instant(value) for value in (observation.window.start_utc, observation.window.end_utc))
    proof_start, proof_end = (_parse_utc_instant(value) for value in (proof.window.start_utc, proof.window.end_utc))
    return (start <= created <= proof_start < proof_end <= end
            and created <= _parse_utc_instant(proof.measured_at) <= _parse_utc_instant(observation.observed_at))


@dataclass(frozen=True)
class TableMeasure:
    """Relevé mesuré d'une voie — ``None`` = non observé, jamais estimé."""

    estimated_rows: int | None = None
    snapshot_rows: int | None = None
    snapshot_published: int | None = None
    loaded_rows: int | None = None
    certification: CertificationMeasure | None = None
    estimated_credits: CreditEvidence | None = None
    measured_credits: CreditEvidence | None = None

    def __post_init__(self) -> None:
        for observation, kind in ((self.estimated_credits, "estimate"), (self.measured_credits, "measured")):
            if observation is not None and (type(observation) is not CreditEvidence or observation.kind != kind):
                raise FleetError("invalid_credit_evidence", "Credit estimate and measurement must remain distinct")


@dataclass(frozen=True)
class FleetEvidence:
    """Tout ce qu'un cycle de progression est autorisé à savoir."""

    prepare_intent_id: str | None = None
    start_checkpoint: JournalCheckpoint | None = None
    history_active: bool = False
    paused: bool = False
    current_checkpoint: JournalCheckpoint | None = None
    committed_tail: JournalCheckpoint | None = None
    receiver_chain: ReceiverChain | None = None
    continuity_proven: bool | None = None
    gap: bool | None = None
    tables: Mapping[str, TableMeasure] = field(default_factory=dict)
    credit_budget: CreditEvidence | None = None

    def __post_init__(self) -> None:
        if self.credit_budget is not None and (type(self.credit_budget) is not CreditEvidence or self.credit_budget.kind != "budget"):
            raise FleetError("invalid_credit_evidence", "An explicit financial budget is required")


@dataclass(frozen=True)
class ProgressionOutcome:
    """Compte rendu borné d'un cycle — aucun secret, aucun chemin."""

    status: str
    reason: str | None
    phase: str | None
    table_phases: Mapping[str, str]
    errors: tuple[str, ...]


def committed_tail(receivers: tuple[CatalogReceiver, ...]) -> JournalCheckpoint | None:
    """Dernière position commise du journal, d'après le catalogue.

    Sur IBM i, le ``last_sequence`` d'un receiver ATTACHED est la prochaine
    position à écrire — la lecture ne la consomme jamais (la convention est
    celle de ``continuous.finite_tail_bootstrap``). La queue commise est donc
    ``last_sequence - 1`` sur le receiver attaché, ou le ``last_sequence``
    final du receiver précédent quand le courant n'a encore rien écrit.
    """

    if not receivers:
        return None
    newest = receivers[-1]
    if newest.status != ATTACHED_STATUS:
        return JournalCheckpoint(newest.name, newest.last_sequence)
    if newest.last_sequence > newest.first_sequence:
        return JournalCheckpoint(newest.name, newest.last_sequence - 1)
    if len(receivers) > 1:
        previous = receivers[-2]
        return JournalCheckpoint(previous.name, previous.last_sequence)
    return None


def receiver_chain(receivers: tuple[CatalogReceiver, ...]) -> ReceiverChain | None:
    """Chaîne domaine issue des bornes réelles cataloguées — jamais inventée."""

    if not receivers:
        return None
    spans = tuple(
        ReceiverSpan(
            receiver=receiver.name,
            first_sequence=receiver.first_sequence,
            # Un receiver attaché continue d'écrire : sa borne cataloguée est
            # l'instantané du relevé — le span reste ouvert au-delà.
            open=receiver.status == ATTACHED_STATUS,
            last_sequence=receiver.last_sequence,
        )
        for receiver in receivers
    )
    return ReceiverChain(spans)


def continuity_verdict(continuity: str | None) -> tuple[bool | None, bool | None]:
    """Verdict catalogue → (continuity_proven, gap) du domaine."""

    if continuity == CONTINUITY_PROVEN:
        return True, False
    if continuity == CONTINUITY_BROKEN:
        return False, True
    return None, None


def public_phase(run: FleetRun, job_phase: str) -> str:
    """Phase publique agrégée : la table la moins avancée, bornée par le job.

    Le runtime job (prepare/history) reste le plancher : une flotte prête dont
    la copie n'a jamais démarré n'est pas « en cours ». Au-delà, la phase la
    moins avancée l'emporte — la flotte n'est LIVE que si toutes les tables le
    sont, et BLOCKED dès qu'une seule l'est.
    """

    phases = [table.phase for table in run.tables]
    if any(phase is Phase.BLOCKED for phase in phases):
        return "BLOCKED"
    active = [phase for phase in phases if phase is not Phase.PAUSED]
    if not active:
        return "PAUSED"
    domain_rank = min(_PHASE_RANK[phase] for phase in active)
    job_rank = _JOB_PHASE_RANK.get(job_phase, 0)
    return _RANK_PUBLIC[max(domain_rank, min(job_rank, len(_RANK_PUBLIC) - 1))]


def public_table_phases(run: FleetRun) -> dict[str, str]:
    """Phase publique par table — le nom domaine, déjà dans le contrat fil."""

    return {table.name: table.phase.value for table in run.tables}


def public_table_states(run: FleetRun) -> dict[str, dict[str, object]]:
    """État public par table : phase réelle et compteurs mesurés.

    ``copied_rows``/``total_rows`` sont les mesures de la copie initiale
    enregistrées par le pilote — ``None`` tant qu'aucune mesure n'existe,
    jamais une valeur inférée.
    """

    return {
        table.name: {
            "phase": table.phase.value,
            "copied_rows": table.copied_rows,
            "total_rows": table.total_rows,
        }
        for table in run.tables
    }


def run_state_document(
    run: FleetRun, *, prepare_intent_id: str, created_at: str | None = None,
    credit_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Document persisté : le run sérialisé, rattaché à sa génération prepare.

    ``created_at`` date la première écriture du run : les preuves de
    certification ne sont admises que si leur mesure est postérieure —
    jamais une preuve d'une génération antérieure.
    """

    created = created_at or datetime.now(timezone.utc).isoformat()
    _validate_credit_state(run, credit_evidence, prepare_intent_id, created)
    return {
        "format_version": RUN_STATE_FORMAT,
        "environment": _fleet.ENVIRONMENT,
        "prepare_intent_id": prepare_intent_id,
        "created_at": created,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fleet": _fleet.serialize_fleet(run),
        "credit_evidence": deepcopy(credit_evidence),
    }


def parse_run_state(document: object) -> tuple[str, FleetRun, str]:
    """Recharge le run persisté ; refuse tout document hors contrat.

    Retourne ``(prepare_intent_id, run, created_at)`` — l'horodatage de
    création borné : absent sur les runs antérieurs au champ, il vaut alors
    ``updated_at``, toujours un instant réel du run jamais une invention.
    """

    if not isinstance(document, Mapping):
        raise FleetError("invalid_run_state", "Unreadable fleet state")
    if document.get("format_version") == "quadringent-fleet-run-state-v1":
        raise FleetError("unverified_credit_state", "Legacy fleet credit units require independent review")
    if document.get("format_version") != RUN_STATE_FORMAT:
        raise FleetError("invalid_run_state", "Unsupported fleet state version")
    if document.get("environment") != _fleet.ENVIRONMENT:
        raise FleetError("invalid_run_state", "Fleet credit evidence belongs to another environment")
    intent = document.get("prepare_intent_id")
    if type(intent) is not str or not intent.strip():
        raise FleetError("invalid_run_state", "Fleet state without generation")
    created = document.get("created_at")
    if type(created) is not str or not created.strip():
        created = document.get("updated_at")
    if type(created) is not str or not created.strip():
        raise FleetError("invalid_run_state", "Fleet state without timestamp")
    run = _fleet.deserialize_fleet(document.get("fleet"))
    _validate_credit_state(run, document.get("credit_evidence"), intent, created)
    return intent, run, created.strip()


def _validate_credit_state(run: FleetRun, value: object, intent: str, created_at: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"budget", "tables"} or not isinstance(value["tables"], Mapping):
        raise FleetError("invalid_credit_evidence", "Persisted fleet costs require financial evidence")
    budget = CreditEvidence.from_dict(value["budget"], "budget")
    if budget.credits != run.credit_budget:
        raise FleetError("invalid_credit_evidence", "Budget differs from its financial evidence")
    expected = {table.name for table in run.tables if table.estimated_credits is not None or table.actual_credits is not None}
    if set(value["tables"]) != expected:
        raise FleetError("invalid_credit_evidence", "Financial evidence must match admitted tables")
    for table in run.tables:
        if table.name not in expected:
            continue
        entry = value["tables"][table.name]
        fields = {key for key, number in (("estimate", table.estimated_credits), ("measured", table.actual_credits)) if number is not None}
        if not isinstance(entry, Mapping) or set(entry) != fields:
            raise FleetError("invalid_credit_evidence", "Missing table credit provenance")
        for key, number in (("estimate", table.estimated_credits), ("measured", table.actual_credits)):
            if number is not None:
                observation = CreditEvidence.from_dict(entry[key], key)
                if observation.credits != number:
                    raise FleetError("invalid_credit_evidence", "Table credits differ from their evidence")
                if key == "measured" and not _measurement_matches_run(observation, intent, table.name, created_at):
                    raise FleetError("invalid_credit_evidence", "Metering does not cover current run/table work")


class FleetProgression:
    """Un cycle = mesures → transitions domaine → persistance atomique.

    ``run_store`` est le ``AtomicJsonStateStore`` de ``fleet-run.json`` ;
    ``evidence_provider`` rend le relevé courant (``None`` quand les sondes
    n'ont encore rien publié). La classe ne connaît ni S3 ni Kubernetes —
    la collecte est câblée par l'appelant.
    """

    def __init__(
        self,
        *,
        plan: Any,
        run_store: Any,
        evidence_provider: Callable[[], FleetEvidence | None],
        emit: Callable[[ProgressionOutcome], None] | None = None,
    ) -> None:
        self._plan = plan
        self._run_store = run_store
        self._evidence_provider = evidence_provider
        self._emit = emit
        self._created_at: str | None = None
        self._credit_evidence: dict[str, object] | None = None

    def tick(self) -> ProgressionOutcome:
        evidence = self._evidence_provider()
        if evidence is None:
            return self._outcome("idle", "evidence_unavailable", None)
        job_phase = "HISTORICAL" if evidence.history_active else "PREPARED"
        try:
            stored = self._load()
        except FleetError as error:
            if error.code == "unverified_credit_state":
                return self._outcome("halted", error.code, None)
            raise
        if stored is not None and stored[0] != evidence.prepare_intent_id:
            # Un prepare plus récent a produit un autre point de départ : le
            # run persisté appartient à une génération morte. On refuse de
            # l'écraser — un opérateur décide, pas le pilote.
            return self._outcome("halted", "stale_prepare_intent", None)
        run = stored[1] if stored is not None else None
        if stored is not None:
            self._created_at = stored[2]
            if evidence.credit_budget is not None and evidence.credit_budget.credits != run.credit_budget:
                return self._outcome("halted", "credit_budget_changed", None)
        if run is None:
            if not evidence.history_active:
                return self._outcome("idle", "history_not_started", None)
            if evidence.start_checkpoint is None:
                return self._outcome("idle", "missing_start_checkpoint", None)
            if evidence.credit_budget is None:
                return self._outcome("idle", "unknown_credit_budget", None)
            run = self._create(evidence)
        if evidence.paused:
            self._save(run, evidence)
            return self._outcome("paused", None, run, job_phase=job_phase)
        errors: list[str] = []
        progressed = False
        for _ in range(_MAX_PASSES):
            moved = False
            admitted, error = self._admit(run, evidence)
            if error is not None:
                errors.append(error)
            if admitted is not run:
                run = admitted
                moved = True
            for table in run.tables:
                try:
                    advanced, error = self._advance(run, table, evidence)
                except FleetError as error:
                    errors.append(f"{table.name}:{error.code}")
                    continue
                if error is not None:
                    errors.append(f"{table.name}:{error}")
                if advanced is not run:
                    run = advanced
                    moved = True
            progressed = progressed or moved
            if not moved:
                break
        self._save(run, evidence)
        return self._outcome(
            "advanced" if progressed else "idle", None, run, errors, job_phase=job_phase
        )

    def _create(self, evidence: FleetEvidence) -> FleetRun:
        if evidence.credit_budget is None:
            raise FleetError("unknown_cost", "Missing explicit fleet credit budget")
        run = _fleet.create_fleet(
            max_concurrency=self._plan.max_concurrency,
            credit_budget=evidence.credit_budget.credits,
        )
        for name in _fleet.MANIFEST:
            run = _fleet.prepare_table(run, name, evidence.start_checkpoint)
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._credit_evidence = {"budget": evidence.credit_budget.to_dict(), "tables": {}}
        return run

    def _admit(self, run: FleetRun, evidence: FleetEvidence) -> tuple[FleetRun, str | None]:
        if not evidence.history_active:
            return run, None
        for _ in range(_fleet.TABLE_COUNT):
            candidates = _fleet.admission_candidates(run)
            if not candidates:
                return run, None
            measure = evidence.tables.get(candidates[0])
            estimate = measure.estimated_credits if measure is not None else None
            if estimate is None:
                return run, f"{candidates[0]}:unknown_cost"
            try:
                run = _fleet.admit_next(run, estimated_credits=estimate.credits)
                self._credit_evidence["tables"][candidates[0]] = {"estimate": estimate.to_dict()}
            except FleetError as error:
                return run, f"{candidates[0]}:{error.code}"
        return run, None

    def _advance(
        self, run: FleetRun, table: TableState, evidence: FleetEvidence
    ) -> tuple[FleetRun, str | None]:
        """Une transition mesurée, ou ``(run, code)`` quand la mesure échoue.

        Le code d'erreur en retour (plutôt qu'une ``FleetError``) préserve
        les transitions déjà acquises du même passage : un ancrage
        ``RECONCILING`` suivi d'une preuve refusée laisse la voie en
        ``RECONCILING``, l'écart déclaré — jamais l'ancrage annulé ni la
        voie figée en ``LIVE`` avec une erreur muette.
        """

        if table.phase is Phase.READY:
            return run, None
        if table.phase is Phase.HISTORICAL:
            measure = evidence.tables.get(table.name)
            if measure is not None and measure.snapshot_rows is not None:
                total = table.total_rows
                if total is None:
                    total = measure.snapshot_published
                run = _fleet.record_history_progress(
                    run,
                    table.name,
                    copied_rows=measure.snapshot_rows,
                    total_rows=total,
                )
                table = _fleet.get_table(run, table.name)
            if table.phase in _fleet.BACKFILL_PHASES:
                run = _fleet.record_journal_evidence(
                    run,
                    table.name,
                    current_checkpoint=evidence.current_checkpoint,
                    journal_tail=evidence.committed_tail,
                    receiver_chain=evidence.receiver_chain,
                    continuity_proven=evidence.continuity_proven,
                    gap=evidence.gap,
                )
            return run, None
        if table.phase is Phase.CATCHING_UP:
            measure = evidence.tables.get(table.name)
            observation = measure.measured_credits if measure is not None else None
            if observation is None or not _measurement_matches_run(observation, evidence.prepare_intent_id, table.name, self._created_at):
                return run, "unknown_cost"
            entry = self._credit_evidence["tables"][table.name]
            if "measured" in entry:
                previous = CreditEvidence.from_dict(entry["measured"], "measured")
                if (_parse_utc_instant(observation.window.start_utc) != _parse_utc_instant(previous.window.start_utc)
                    or _parse_utc_instant(observation.window.end_utc) < _parse_utc_instant(previous.window.end_utc)
                    or _parse_utc_instant(observation.observed_at) < _parse_utc_instant(previous.observed_at)
                    or _parse_utc_instant(observation.work_proof.measured_at) < _parse_utc_instant(previous.work_proof.measured_at)
                    or _parse_utc_instant(observation.work_proof.window.end_utc) < _parse_utc_instant(previous.work_proof.window.end_utc)):
                    return run, "contradictory_cost_window"
            if observation.credits != table.actual_credits:
                run = _fleet.record_actual_cost(run, table.name, observation.credits)
                if _fleet.get_table(run, table.name).phase is Phase.BLOCKED:
                    entry["measured"] = observation.to_dict()
                    return run, _fleet.get_table(run, table.name).blocked_reason
            entry["measured"] = observation.to_dict()
            try:
                run = _fleet.record_journal_evidence(
                    run,
                    table.name,
                    current_checkpoint=evidence.current_checkpoint,
                    journal_tail=evidence.committed_tail,
                    receiver_chain=evidence.receiver_chain,
                    continuity_proven=evidence.continuity_proven,
                    gap=evidence.gap,
                )
            except FleetError as error:
                return run, error.code
            return run, "unknown_cost" if _fleet.get_table(run, table.name).actual_credits is None else None
        if table.phase in {Phase.LIVE, Phase.RECONCILING}:
            return self._certify(run, table, evidence)
        return run, None

    def _certify(
        self, run: FleetRun, table: TableState, evidence: FleetEvidence
    ) -> tuple[FleetRun, str | None]:
        """LIVE→RECONCILING→CERTIFIED, conduit uniquement par la mesure.

        La preuve vient de ``console-proof.json`` (``certify.tables``) :
        la fin de sa fenêtre, jamais avant la création du run courant. Une
        divergence est re-tentée à chaque cycle — les appels domaine sont
        purs, la déclaration de l'écart reste visible tant qu'il dure ; un
        relevé corrigé de la sonde fait basculer dès sa publication. En
        ``RECONCILING``, une fenêtre différente se ré-ancre : la
        certification exige toujours que la preuve réponde exactement à la
        fenêtre déclarée.
        """

        measure = evidence.tables.get(table.name)
        cert = measure.certification if measure is not None else None
        if cert is None:
            return run, None
        created_at = _parse_utc_instant(self._created_at)
        measured_at = _parse_utc_instant(cert.measured_at)
        if measured_at is None:
            return run, None
        if created_at is not None and measured_at < created_at:
            return run, None
        if table.actual_credits is None:
            return run, None
        if table.proof_window != cert.window:
            run = _fleet.begin_reconciliation(run, table.name, cert.window)
        proof = ReconciliationProof(
            window=cert.window,
            source_count=cert.source_count,
            target_count=cert.target_count,
            missing=cert.missing,
            extra=cert.extra,
            duplicates=cert.duplicates,
            source_hash=cert.source_hash,
            target_hash=cert.target_hash,
            destination_freshness_seconds=cert.freshness_seconds,
            freshness_slo_seconds=CERTIFICATION_FRESHNESS_SLO_SECONDS,
            latency_seconds=cert.latency_seconds,
            throughput_rows_per_second=cert.throughput_rows_per_second,
            cost_units=table.actual_credits,
        )
        try:
            return _fleet.certify_table(run, table.name, proof), None
        except FleetError as error:
            # L'ancrage est un fait acquis : la voie reste RECONCILING,
            # l'écart mesuré est déclaré à chaque cycle — la prochaine
            # mesure corrigée de la sonde refera la tentative.
            return run, error.code

    def _load(self) -> tuple[str, FleetRun, str] | None:
        document = self._run_store.load()
        if document is None:
            return None
        result = parse_run_state(document)
        self._credit_evidence = deepcopy(document["credit_evidence"])
        return result

    def _save(self, run: FleetRun, evidence: FleetEvidence) -> None:
        if evidence.prepare_intent_id is None:
            return
        self._run_store.save(
            run_state_document(
                run,
                prepare_intent_id=evidence.prepare_intent_id,
                created_at=self._created_at,
                credit_evidence=self._credit_evidence,
            )
        )

    def _outcome(
        self,
        status: str,
        reason: str | None,
        run: FleetRun | None,
        errors: list[str] | tuple[str, ...] = (),
        *,
        job_phase: str = "NOT_PREPARED",
    ) -> ProgressionOutcome:
        outcome = ProgressionOutcome(
            status=status,
            reason=reason,
            phase=None if run is None else public_phase(run, job_phase),
            table_phases={} if run is None else public_table_phases(run),
            errors=tuple(errors),
        )
        if self._emit is not None:
            try:
                self._emit(outcome)
            except Exception:
                pass
        return outcome


def _parse_utc_instant(value: object) -> datetime | None:
    """Instant UTC typé, ou ``None`` — jamais une comparaison de chaînes."""

    if type(value) is not str or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)
