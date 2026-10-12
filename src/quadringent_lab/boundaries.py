"""Fakes for the replaced boundaries.

Each class implements an existing injectable protocol of the control plane
(``source_probe``, ``table_discovery_client``, ``destination_verifier``,
``pipeline_executor``, ``pipeline_observation_provider`` with ``.evidence``,
``log_source``, ``costs_provider``). They are deterministic, synthetic and
never open a socket. Secrets received from the product are never stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from quadringent.table_discovery import DiscoveredTable
from quadringent_control_plane.v2.executor.kubernetes import ExecutorError
from quadringent_control_plane.v2.services.costs import CostScope, CostSnapshot
from quadringent_control_plane.v2.services.destination_verifier import (
    DestinationVerificationResult,
    DestinationVerifierRequest,
    VerificationOutcome,
)
from quadringent_control_plane.v2.services.evidence import EvidenceStage, PipelineEvidence, absent_evidence
from quadringent_control_plane.v2.services.logs import RawLogEntry
from quadringent_control_plane.v2.services.observation import (
    MetricPoint,
    MetricsSeries,
    MetricsWindow,
    PipelineObservation,
    absent_observation,
    empty_metrics_series,
)
from quadringent_control_plane.v2.services.source_probe import (
    ProbeOutcome,
    SourceProbeRequest,
    SourceProbeResult,
    build_probe_result,
)

from . import LAB_PROVENANCE
from . import fixtures
from .state import LabState

FRESH_AGE = timedelta(seconds=20)
STALE_AGE = timedelta(hours=3)
HISTORICAL_AGE = timedelta(days=3)

LAB_FINGERPRINT = "LA:B0:FI:XT:UR:E0:00:00:00:00:00:00:00:00:00:00"
LAB_CERTIFICATE_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "LAB-FIXTURE-NOT-A-REAL-CERTIFICATE\n"
    "-----END CERTIFICATE-----"
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --- Source probe (IBM i network, TLS, authentication) ------------------------
class LabSourceProbe:
    def __init__(self, state: LabState) -> None:
        self._state = state

    def probe(self, request: SourceProbeRequest) -> SourceProbeResult:
        mode = self._state.conditions.probe
        # Ledger: which mode answered; never host credentials or the secret.
        self._state.record("source_probe", mode=mode, tls_trust=request.tls_trust)
        if mode == "network_down":
            return build_probe_result(
                network=ProbeOutcome(False, "TCP connection refused (laboratory-injected condition)."),
                tls=ProbeOutcome(False, "Not attempted: no network path."),
                tls_fingerprint=None,
                authentication=ProbeOutcome(False, "Not attempted: no network path."),
                ibmi_version=None,
                qtimzon=None,
            )
        if mode == "tls_invalid":
            return build_probe_result(
                network=ProbeOutcome(True, "TCP connection established (laboratory fixture)."),
                tls=ProbeOutcome(False, "Certificate chain not trusted (laboratory-injected condition)."),
                tls_fingerprint=LAB_FINGERPRINT,
                authentication=ProbeOutcome(False, "Not attempted: TLS not established."),
                ibmi_version=None,
                qtimzon=None,
                tls_trust="unknown",
                tls_certificate_pem=LAB_CERTIFICATE_PEM,
            )
        if mode == "auth_denied":
            return build_probe_result(
                network=ProbeOutcome(True, "TCP connection established (laboratory fixture)."),
                tls=ProbeOutcome(True, "TLS established with a trusted chain (laboratory fixture)."),
                tls_fingerprint=LAB_FINGERPRINT,
                authentication=ProbeOutcome(False, "Authentication refused (laboratory-injected condition)."),
                ibmi_version=None,
                qtimzon=None,
                tls_trust="system",
            )
        return build_probe_result(
            network=ProbeOutcome(True, "TCP connection established (laboratory fixture)."),
            tls=ProbeOutcome(True, "TLS established with a trusted chain (laboratory fixture)."),
            tls_fingerprint=LAB_FINGERPRINT,
            authentication=ProbeOutcome(True, "Authentication accepted (laboratory fixture)."),
            ibmi_version="V7R5M0",
            qtimzon="QP0100CET",
            tls_trust="system",
        )


# --- Table discovery ----------------------------------------------------------
def _table(
    name: str,
    *,
    key: tuple[str, ...] = ("ID",),
    journaled: bool = True,
    images: str | None = "*BOTH",
    rows: int = 1000,
    text: str | None = None,
) -> DiscoveredTable:
    return DiscoveredTable(
        library=fixtures.LIBRARY,
        system_name=name,
        sql_name=name,
        text=text,
        row_count=rows,
        size_bytes=rows * 128,
        has_key=bool(key),
        key_columns=key,
        journaled=journaled,
        journal_library=fixtures.LIBRARY if journaled else None,
        journal_name="LABJRN" if journaled else None,
        images=images if journaled else None,
        omitted=False,
    )


class LabTableDiscovery:
    """Catalog of a synthetic IBM i library; the extra table depends on the scenario."""

    def __init__(self, state: LabState) -> None:
        self._state = state

    def catalog(self) -> tuple[DiscoveredTable, ...]:
        conditions = self._state.conditions
        tables = [
            _table("ORDERS", key=("ORDER_ID",), rows=120_000),
            _table(
                "CUSTOMERS",
                key=("CUSTNO",),
                rows=4_800,
                text=fixtures.CANARY_TABLE_COMMENT if conditions.canaries else None,
            ),
        ]
        if conditions.discovery == "journal_absent":
            tables.append(_table("LEDGER_RAW", journaled=False, rows=900))
        elif conditions.discovery == "journal_config":
            tables.append(_table("LEDGER_AFTER", images="*AFTER", rows=900))
        elif conditions.discovery == "no_key":
            tables.append(_table("LEDGER_NOKEY", key=(), rows=900))
        return tuple(tables)

    def discover(
        self, *, libraries: Sequence[str] | None, limit: int, search: str | None
    ) -> tuple[DiscoveredTable, ...]:
        self._state.record("table_discovery", mode=self._state.conditions.discovery)
        tables = self.catalog()
        if libraries:
            wanted = {library.upper() for library in libraries}
            tables = tuple(table for table in tables if table.library in wanted)
        if search:
            tables = tuple(table for table in tables if search.upper() in table.system_name)
        return tables[:limit]


# --- Destination verifier (Snowflake) ------------------------------------------
class LabDestinationVerifier:
    def __init__(self, state: LabState) -> None:
        self._state = state

    def verify(self, request: DestinationVerifierRequest) -> DestinationVerificationResult:
        mode = self._state.conditions.verifier
        # The private key in ``request`` is deliberately not recorded.
        self._state.record("destination_verifier", mode=mode, service_user=request.service_user)
        ok = VerificationOutcome(True, "Accepted (laboratory fixture).")
        if mode == "connection_failed":
            failed = VerificationOutcome(False, "Connection refused (laboratory-injected condition).")
            skipped = VerificationOutcome(False, "Not attempted: no connection.")
            return DestinationVerificationResult(failed, skipped, skipped, skipped, skipped, skipped)
        if mode == "insufficient_privileges":
            return DestinationVerificationResult(
                connection=ok,
                role=ok,
                warehouse=ok,
                database=ok,
                schema=ok,
                load_privileges=VerificationOutcome(
                    False, "Role cannot create tables in the target schema (laboratory-injected condition)."
                ),
            )
        return DestinationVerificationResult(ok, ok, ok, ok, ok, ok)


# --- Pipeline executor (Kubernetes) ----------------------------------------------
class LabPipelineExecutor:
    """Records every execution. ``ok`` accepts any transition without effect."""

    def __init__(self, state: LabState) -> None:
        self._state = state

    def execute(self, *, pipeline_id: str, event: str, **_ignored: object) -> None:
        mode = self._state.conditions.executor
        self._state.record("pipeline_executor", pipeline_id=pipeline_id, event=event, outcome=mode)
        if mode == "unavailable":
            raise ExecutorError(
                "executor_unavailable", "Pipeline executor unavailable (laboratory-injected condition)."
            )


# --- Observation + evidence ---------------------------------------------------
_OBSERVED_STATE = {fixtures.PIPELINE_ORDERS: "live", fixtures.PIPELINE_CUSTOMERS: "copying"}
_LABELS = {
    fixtures.PIPELINE_ORDERS: "LABLIB.ORDERS",
    fixtures.PIPELINE_CUSTOMERS: "LABLIB.CUSTOMERS",
    fixtures.PIPELINE_OTHER_ORDERS: "OTHERLIB.ORDERS (other organization)",
}


class LabObservationProvider:
    """Implements ``observe``/``metrics`` and the optional ``evidence`` reader."""

    def __init__(self, state: LabState) -> None:
        self._state = state

    def _mode(self, pipeline_id: str) -> str:
        if pipeline_id == fixtures.PIPELINE_OTHER_ORDERS:
            return "simulation"
        return self._state.conditions.evidence_for(pipeline_id)

    @staticmethod
    def _age(mode: str) -> timedelta:
        return {"stale": STALE_AGE, "historical": HISTORICAL_AGE}.get(mode, FRESH_AGE)

    def observe(self, pipeline_id: str) -> PipelineObservation:
        mode = self._mode(pipeline_id)
        if mode == "absent":
            return absent_observation(pipeline_id, reason="Laboratory condition: no observation collected.")
        collected = datetime.now(timezone.utc) - self._age(mode)
        return PipelineObservation(
            observed_state=_OBSERVED_STATE.get(pipeline_id, "live"),
            lag_seconds=2.0,
            throughput_rows_per_second=41.5,
            rows_source=120_000,
            rows_destination=119_998,
            last_arrival_at=_iso(collected),
            collected_at=_iso(collected),
            history_lag_seconds=3.0,
            mirror_lag_seconds=4.0,
        )

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        mode = self._mode(pipeline_id)
        if mode == "absent":
            return empty_metrics_series(window, reason="Laboratory condition: no measurement collected.")
        step = timedelta(minutes=5) if window == "1h" else timedelta(hours=1)
        count = 12 if window == "1h" else 24
        end = datetime.now(timezone.utc) - self._age(mode)
        points = tuple(
            MetricPoint(
                at=_iso(end - step * (count - 1 - index)),
                lag_seconds=float(2 + index % 4),
                throughput_rows_per_second=float(40 + index % 5),
            )
            for index in range(count)
        )
        return MetricsSeries(
            window=window,
            points=points,
            provenance=LAB_PROVENANCE,
            freshness="synthetic (laboratory fixture)",
            collected_at=_iso(end),
        )

    def evidence(self, pipeline_id: str) -> PipelineEvidence:
        mode = self._mode(pipeline_id)
        label = _LABELS.get(pipeline_id, pipeline_id)
        if mode == "absent":
            return absent_evidence(pipeline_id, reason=f"Laboratory condition: no evidence collected for {label}.")
        now = datetime.now(timezone.utc)
        fresh = _iso(now - FRESH_AGE)
        old = _iso(now - STALE_AGE)
        kind = "live" if mode in ("live", "stale", "reading_only") else mode
        injected = " Laboratory-injected live condition, not a measurement." if kind == "live" else ""
        base_detail = f"Synthetic laboratory fixture for {label}; no IBM i or Snowflake was read.{injected}"
        delivery_detail = base_detail
        if self._state.conditions.canaries and pipeline_id == fixtures.PIPELINE_ORDERS:
            delivery_detail = (
                f"{base_detail} Last batch rejected row "
                f"{{\"CUSTNO\": \"{fixtures.CANARY_ROW_VALUE}\", \"NAME\": \"x\"}} "
                f"password={fixtures.CANARY_EVIDENCE_SECRET}"
            )
        reading = EvidenceStage("observed", fresh, 1_200, 2.0, None, base_detail)
        delivery = EvidenceStage("observed", fresh, 1_198, 4.0, None, delivery_detail)
        reconciliation = EvidenceStage("observed", fresh, 1_198, None, 0, base_detail)
        if mode == "historical":
            past = _iso(now - HISTORICAL_AGE)
            reading = EvidenceStage("observed", past, 1_200, 2.0, None, base_detail)
            delivery = EvidenceStage("observed", past, 1_198, 4.0, None, delivery_detail)
            reconciliation = EvidenceStage("observed", past, 1_198, None, 0, base_detail)
        elif mode == "stale":
            delivery = EvidenceStage("stale", old, 1_198, 4.0, None, delivery_detail)
            reconciliation = EvidenceStage("stale", old, 1_198, None, 0, base_detail)
        elif mode == "reading_only":
            missing = f"Laboratory condition: nothing measured for {label} at this stage."
            delivery = EvidenceStage(detail=missing)
            reconciliation = EvidenceStage(detail=missing)
        collected_at = _iso(now - (HISTORICAL_AGE if mode == "historical" else FRESH_AGE))
        return PipelineEvidence(
            pipeline_id=pipeline_id,
            provenance=LAB_PROVENANCE,
            evidence_kind=kind,
            collected_at=collected_at,
            reading=reading,
            delivery=delivery,
            reconciliation=reconciliation,
        )


# --- Logs and costs -----------------------------------------------------------
class LabLogSource:
    def __init__(self, state: LabState) -> None:
        self._state = state

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]:
        now = datetime.now(timezone.utc)
        label = _LABELS.get(pipeline_id, pipeline_id)
        entries = [
            RawLogEntry(_iso(now - timedelta(minutes=3)), "info", f"Batch of 100 events applied to {label}."),
            RawLogEntry(_iso(now - timedelta(minutes=2)), "info", f"Checkpoint stored for {label}."),
            RawLogEntry(
                _iso(now - timedelta(minutes=1)), "warning", "Receiver rotation observed; reading resumed.", "inc_lab_1"
            ),
        ]
        if self._state.conditions.canaries:
            entries.append(
                RawLogEntry(
                    _iso(now - timedelta(seconds=30)),
                    "error",
                    f"Retry after error password={fixtures.CANARY_LOG_SECRET} "
                    f"on row {{\"CUSTNO\": \"{fixtures.CANARY_ROW_VALUE}\", \"NAME\": \"x\"}}",
                    "inc_lab_2",
                )
            )
        return tuple(entries)


class LabCostsProvider:
    def get(self, scope: CostScope, id_: str, *, window: str | None) -> CostSnapshot:
        return CostSnapshot(
            scope=scope,
            id=id_,
            window=window,
            status="estimated",
            amount=0.42,
            currency="USD",
            basis="average_throughput_estimate",
            collected_at=_iso(datetime.now(timezone.utc) - FRESH_AGE),
            reason="Synthetic laboratory fixture.",
        )
