"""Télémétrie du chargeur Snowflake, attribuée à une table v2 précise.

Le chargeur est partagé par destination. Les lignes de ses pods ne deviennent
visibles dans un pipeline que si elles portent le nom exact de sa table
(formats historiques) ou le tag de son identité interne (durées du cycle).
Seuls les formats structurés émis par le chargeur sont acceptés ; toute
autre ligne est écartée avant de franchir l'API.

``miroir_secondes`` mesure le délai IBM i -> MERGE du dernier lot traité.
Le relevé ``retard mesuré`` suit un lot traité et donne l'âge de sa dernière
mutation visible dans Snowflake ; il ne prouve pas le retard d'un flux inactif.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Literal

from quadringent.snowflake_streaming_loader import LOADER_CYCLE_FORMAT, LOADER_CYCLE_STAGES, loader_table_tag

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from ...k8s_pods import KubernetesPodsClient, PodsApiError
from .. import schema
from .logs import RawLogEntry
from .evidence import EvidenceStage, PipelineEvidence, absent_evidence
from .observation import (
    MetricPoint,
    MetricsSeries,
    MetricsWindow,
    PipelineObservation,
    absent_observation,
    empty_metrics_series,
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DELIVERY = re.compile(
    r"\b(?:delivery|livraison) table=(?P<table>[A-Za-z][A-Za-z0-9_]*) "
    "(?:new_events|événements_nouveaux)=(?P<count>\\d+) (?:mirror_seconds|miroir_secondes)=(?P<lat>\\d+(?:\\.\\d+)?)\\b"
)
_LAG = re.compile(
    "\\b(?:measured lag|retard mesuré) table=(?P<table>[A-Za-z][A-Za-z0-9_]*) "
    r"(?:history|historique)=(?P<history>None|\d+(?:\.\d+)?)s "
    r"(?:mirror|miroir)=(?P<mirror>None|\d+(?:\.\d+)?)s\b"
)
_LOAD = re.compile(
    r"\btable=(?P<table>[A-Za-z][A-Za-z0-9_]*) "
    "(?:batches|lots)=(?P<batches>\\d+) (?:events|événements)=(?P<count>\\d+)\\b"
)
_TAIL_LINES = 2000
_MAX_PODS = 3
_NO_DELIVERY = "no mirror delivery observed in the retained Kubernetes log"


@dataclass(frozen=True)
class LoaderIdentity:
    destination_id: str
    table_name: str
    table_id: str | None = None


def resolve_loader_identity(engine: Engine, pipeline_id: str) -> LoaderIdentity | None:
    """La base v2 est l'unique correspondance pipeline -> table/destination."""

    with engine.connect() as connection:
        row = connection.execute(
            select(schema.pipelines.c.destination_id, schema.tables.c.table_name, schema.tables.c.id)
            .select_from(schema.pipelines.join(schema.tables, schema.pipelines.c.table_id == schema.tables.c.id))
            .where(schema.pipelines.c.id == pipeline_id)
        ).first()
        if row is not None:
            count = connection.execute(
                select(func.count(func.distinct(schema.tables.c.id)))
                .select_from(schema.pipelines.join(schema.tables, schema.pipelines.c.table_id == schema.tables.c.id))
                .where(schema.pipelines.c.destination_id == row[0],
                       func.upper(func.trim(schema.tables.c.table_name)) == row[1].strip().upper())
            ).scalar_one()
            # Legacy delivery logs contain a basename, not the source library.
            if count != 1:
                return None
    if row is None:
        return None
    return LoaderIdentity(destination_id=row[0], table_name=row[1], table_id=row[2])


@dataclass(frozen=True)
class _Fact:
    at: datetime
    kind: Literal["delivery", "lag", "load", "stages"]
    message: str
    count: int | None = None
    mirror_seconds: float | None = None
    history_seconds: float | None = None


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _number(value: str) -> float | None:
    return None if value == "None" else float(value)


def _cycle_fact(at: datetime, raw: str, expected_tag: str | None) -> _Fact | None:
    """N'expose que les champs numériques et horodatés du contrat borné."""

    if expected_tag is None or len(raw) > 4096:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    keys = {"format", "table_tag", "started_at", "finished_at", "cycle_ms", "stages_ms", "status", "failed_stage"}
    if not isinstance(data, dict) or data.keys() != keys:
        return None
    if data["format"] != LOADER_CYCLE_FORMAT or data["table_tag"] != expected_tag:
        return None
    start, end = data["started_at"], data["finished_at"]
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    started, finished = _time(start), _time(end)
    if started is None or finished is None or finished < started:
        return None
    stages = data["stages_ms"]
    if not isinstance(stages, dict) or stages.keys() != set(LOADER_CYCLE_STAGES):
        return None

    def duration(value: object) -> bool:
        try:
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            return False

    if not duration(data["cycle_ms"]) or any(value is not None and not duration(value) for value in stages.values()):
        return None
    if sum(value or 0 for value in stages.values()) > data["cycle_ms"] + 0.001:
        return None
    status, failed = data["status"], data["failed_stage"]
    if status == "success":
        if failed is not None or all(stages[name] is None for name in LOADER_CYCLE_STAGES if name != "discovery_raw"):
            return None
    elif status == "failed":
        if failed not in (*LOADER_CYCLE_STAGES, "prepare") or (failed != "prepare" and stages[failed] is None):
            return None
    else:
        return None
    values = ", ".join(f"{name}={float(stages[name])} ms" if stages[name] is not None else f"{name}=not measured"
                       for name in LOADER_CYCLE_STAGES)
    # On reconstruit le texte : aucune chaîne libre provenant du pod n'entre dans l'API.
    start_utc = started.isoformat().replace("+00:00", "Z")
    end_utc = finished.isoformat().replace("+00:00", "Z")
    outcome = "passed" if status == "success" else f"failed stage {failed}"
    return _Fact(at, "stages", f"Streaming cycle {outcome} : start {start_utc}, end {end_utc}, "
                 f"loader duration={float(data['cycle_ms'])} ms ; {values}")


def _parse(line: str, table_name: str, expected_tag: str | None = None) -> _Fact | None:
    at_raw, sep, message = line.partition(" ")
    at = _time(at_raw) if sep else None
    if at is None:
        return None
    _, marker, cycle = message.partition("loader_cycle ")
    if marker:
        return _cycle_fact(at, cycle, expected_tag)
    delivery = _DELIVERY.search(message)
    if delivery is not None and delivery["table"].upper() == table_name:
        count = int(delivery["count"])
        latency = float(delivery["lat"])
        return _Fact(at, "delivery", f"Mirror delivery: {count} new events, {latency} s", count, latency)
    lag = _LAG.search(message)
    if lag is not None and lag["table"].upper() == f"{table_name}_HISTORY":
        history = _number(lag["history"])
        mirror = _number(lag["mirror"])
        return _Fact(
            at,
            "lag",
            f"Last mutation age: history {lag['history']} s, mirror {lag['mirror']} s",
            mirror_seconds=mirror,
            history_seconds=history,
        )
    loaded = _LOAD.search(message)
    if loaded is not None and loaded["table"].upper() == table_name:
        batches = int(loaded["batches"])
        count = int(loaded["count"])
        return _Fact(at, "load", f"Chargement : {batches} lots, {count} new events", count)
    return None


class KubernetesLoaderTelemetry:
    """Source de métriques et journaux bornée aux pods du chargeur."""

    def __init__(
        self,
        pods_client: KubernetesPodsClient,
        *,
        resolve: Callable[[str], LoaderIdentity | None],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._pods = pods_client
        self._resolve = resolve
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _facts(self, pipeline_id: str) -> tuple[_Fact, ...]:
        identity = self._resolve(pipeline_id)
        if identity is None:
            return ()
        table_name = identity.table_name.strip().upper()
        if not _IDENTIFIER.fullmatch(table_name) or not _LABEL.fullmatch(identity.destination_id):
            return ()
        expected_tag = loader_table_tag(identity.table_id) if identity.table_id else None
        selector = (
            "quadringent.io/component=destination-loader,"
            f"quadringent.io/destination-id={identity.destination_id}"
        )
        pods = self._pods.list_pod_names(label_selector=selector, limit=_MAX_PODS)
        facts: list[_Fact] = []
        for pod in pods:
            raw = self._pods.read_pod_log(pod, since_time=None, tail_lines=_TAIL_LINES)
            if raw is None:
                continue
            for line in raw.splitlines():
                fact = _parse(line, table_name, expected_tag)
                if fact is not None:
                    facts.append(fact)
        facts.sort(key=lambda fact: fact.at)
        return tuple(facts)

    def observe(self, pipeline_id: str) -> PipelineObservation:
        try:
            facts = self._facts(pipeline_id)
        except PodsApiError as error:
            return absent_observation(pipeline_id, reason=f"Kubernetes log is unavailable ({error.code})")
        now = self._now()
        recent = [fact for fact in facts if fact.at >= now - timedelta(hours=1)]
        deliveries = [fact for fact in recent if fact.kind == "delivery"]
        lags = [fact for fact in recent if fact.kind == "lag"]
        throughput = _rate(deliveries[-2], deliveries[-1]) if len(deliveries) >= 2 else None
        latest_lag = lags[-1] if lags else None
        latest_delivery = deliveries[-1] if deliveries else None
        reason = "no measurement attributable to this table in the recent Kubernetes log"
        absent = {
            "observed_state": "pod state not measured by this source",
            "lag_seconds": "source -> mirror lag not measured while idle",
            "rows_source": "source total not measured by the loader",
            "rows_destination": "destination total not measured by the log",
        }
        if throughput is None:
            absent["throughput_rows_per_second"] = "two recent deliveries are required to calculate throughput"
        if latest_delivery is None:
            absent["last_arrival_at"] = reason
        if latest_lag is None or latest_lag.history_seconds is None:
            absent["history_lag_seconds"] = reason
        if latest_lag is None or latest_lag.mirror_seconds is None:
            absent["mirror_lag_seconds"] = reason
        return PipelineObservation(
            observed_state=None,
            lag_seconds=None,
            throughput_rows_per_second=throughput,
            rows_source=None,
            rows_destination=None,
            last_arrival_at=latest_delivery.at.isoformat().replace("+00:00", "Z") if latest_delivery else None,
            collected_at=recent[-1].at.isoformat().replace("+00:00", "Z") if recent else None,
            history_lag_seconds=latest_lag.history_seconds if latest_lag else None,
            mirror_lag_seconds=latest_lag.mirror_seconds if latest_lag else None,
            absent_reasons=absent,
        )

    def evidence(self, pipeline_id: str) -> PipelineEvidence:
        try:
            facts = self._facts(pipeline_id)
        except PodsApiError as error:
            return absent_evidence(pipeline_id, reason=f"Kubernetes log is unavailable ({error.code})")
        now = self._now()
        deliveries = [fact for fact in facts if fact.kind == "delivery" and fact.at <= now]
        if not deliveries:
            return absent_evidence(pipeline_id, reason=_NO_DELIVERY)
        latest = deliveries[-1]
        return PipelineEvidence(
            pipeline_id, provenance="kubernetes_loader_log", evidence_kind="live",
            collected_at=now.isoformat().replace("+00:00", "Z"),
            reading=EvidenceStage(detail="Journal reading is not measured by the loader log."),
            delivery=EvidenceStage(
                state="observed" if now - latest.at <= timedelta(minutes=5) else "stale",
                observed_at=latest.at.isoformat().replace("+00:00", "Z"),
                event_count=latest.count, lag_seconds=latest.mirror_seconds,
                detail="Last delivered batch for this table; limited to retained loader logs, not a destination total.",
            ),
            reconciliation=EvidenceStage(
                detail="Complete reconciliation and duplicate counts require an independent destination read."
            ),
        )

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        try:
            facts = self._facts(pipeline_id)
        except PodsApiError as error:
            return empty_metrics_series(window, reason=f"Kubernetes log is unavailable ({error.code})")
        cutoff = self._now() - timedelta(hours=1 if window == "1h" else 24)
        deliveries = [fact for fact in facts if fact.kind == "delivery" and fact.at >= cutoff]
        if not deliveries:
            return empty_metrics_series(window, reason=_NO_DELIVERY)
        points = []
        for index, fact in enumerate(deliveries):
            rate = _rate(deliveries[index - 1], fact) if index else None
            points.append(
                MetricPoint(
                    at=fact.at.isoformat().replace("+00:00", "Z"),
                    lag_seconds=fact.mirror_seconds,
                    throughput_rows_per_second=rate,
                )
            )
        return MetricsSeries(
            window=window,
            points=tuple(points),
            provenance="journal_chargeur_kubernetes",
            freshness="limited to the last 2000 pod log lines",
            collected_at=self._now().isoformat().replace("+00:00", "Z"),
            reason="Lag is the IBM i → MERGE delay of delivered batches; the series is limited to the retained log.",
        )

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]:
        try:
            facts = self._facts(pipeline_id)
        except PodsApiError:
            return ()
        cutoff = _time(since) if since else None
        return tuple(
            RawLogEntry(
                at=fact.at.isoformat().replace("+00:00", "Z"),
                level="info",
                message=fact.message,
            )
            for fact in facts
            if cutoff is None or fact.at >= cutoff
        )


def _rate(previous: _Fact, current: _Fact) -> float | None:
    elapsed = (current.at - previous.at).total_seconds()
    return current.count / elapsed if elapsed > 0 and current.count is not None else None
