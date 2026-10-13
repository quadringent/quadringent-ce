"""Bounded local metadata for troubleshooting; never a live health probe."""

from datetime import datetime, timezone
import hmac
from importlib.metadata import PackageNotFoundError, version
import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .. import schema
from .state_machine import DECLARED_STATES

FORMAT_VERSION = "quadringent-diagnostic-v1"
MAX_PIPELINES = 500
_OBSERVED_STATES = frozenset({
    "healthy", "degraded", "incident", "unknown", "planned_stop", "awaiting_resume",
    "running", "failed", "absent", "not_started", "copying", "live", "paused", "attention", "stopped",
})


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def _package_version() -> str | None:
    try:
        value = version("quadringent")
    except PackageNotFoundError:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9.+-]{1,64}", value) else None


class DiagnosticsService:
    def __init__(self, engine: Engine, *, org_id: str) -> None:
        self._engine = engine
        self._org_id = org_id

    def snapshot(self) -> dict[str, object]:
        started = datetime.now(timezone.utc)
        sources, destinations, tables, pipelines = schema.sources, schema.destinations, schema.tables, schema.pipelines
        joined = pipelines.join(tables).join(sources).join(destinations)
        scope = (sources.c.org_id == self._org_id) & (destinations.c.org_id == self._org_id)
        with self._engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN")
                elif connection.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                counts = {
                    "sources": connection.scalar(select(func.count()).select_from(sources).where(sources.c.org_id == self._org_id)),
                    "destinations": connection.scalar(select(func.count()).select_from(destinations).where(destinations.c.org_id == self._org_id)),
                    "tables": connection.scalar(select(func.count()).select_from(tables.join(sources)).where(sources.c.org_id == self._org_id)),
                    "pipelines": connection.scalar(select(func.count()).select_from(joined).where(scope)),
                }
                # Select only allowlisted metadata; never fetch ciphertext or free text.
                rows = connection.execute(select(
                    pipelines.c.id, tables.c.source_id, pipelines.c.destination_id,
                    pipelines.c.declared_state, pipelines.c.last_observed_state, pipelines.c.last_observed_at,
                ).select_from(joined).where(scope).order_by(pipelines.c.id).limit(MAX_PIPELINES)).mappings().all()
        finished = datetime.now(timezone.utc)
        alias_key = secrets.token_bytes(32)
        def reference(kind: str, value: str) -> str:
            return hmac.new(alias_key, f"{kind}\0{value}".encode(), "sha256").hexdigest()
        items = []
        for row in rows:
            at = row["last_observed_at"]
            moment = None if at is None else at.replace(tzinfo=at.tzinfo or timezone.utc).astimezone(timezone.utc)
            quality = "timestamp_missing" if moment is None else "future_clock_untrusted" if moment > finished else "unattributed_cached"
            items.append({
                "pipeline_ref": reference("pipeline", row["id"]),
                "source_ref": reference("source", row["source_id"]),
                "destination_ref": reference("destination", row["destination_id"]),
                "declared_state": row["declared_state"] if row["declared_state"] in DECLARED_STATES else None,
                "stored_observation": {
                    "state": row["last_observed_state"] if row["last_observed_state"] in _OBSERVED_STATES else None,
                    "last_state_change_recorded_at": _timestamp(at), "quality": quality,
                    "provenance": "stored_control_plane_state", "measurement_provenance": "not_stored",
                },
                "delivery_evidence": "not_collected",
            })
        return {
            "format_version": FORMAT_VERSION,
            "package_version": _package_version(),
            "generated_at": _timestamp(finished),
            "database_backend": self._engine.dialect.name,
            "collection": {"mode": "local_database_snapshot", "started_at": _timestamp(started), "finished_at": _timestamp(finished), "live_checks_performed": False},
            "counts": counts,
            "pipeline_inventory": {"items": items, "limit": MAX_PIPELINES, "complete": counts["pipelines"] == len(items)},
            "not_collected": ["source_connectivity", "delivery_evidence", "reconciliation", "metrics", "costs", "logs"],
            "limitations": [
                "Generation time is not the time of a source or delivery measurement.",
                "Stored observation time records a state change, not the most recent poll; its provenance is not stored.",
                "No live probe, provider request, credential decryption or pipeline action was performed.",
                "Names, endpoints, account identifiers, secrets, SQL, row payloads and raw logs are excluded; references are temporary aliases within this file.",
            ],
        }
