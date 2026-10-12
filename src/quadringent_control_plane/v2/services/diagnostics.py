"""Control plane diagnostics: what the v2 records and evidence establish.

Read-only by construction: this service only runs SELECT statements scoped to
the organization (sources, destinations, tables through their source,
pipelines through their table's source) and reads evidence from the
observation provider. It never refreshes observations, calls the executor,
starts a probe, writes audit records or changes a state.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from quadringent.diagnostics.model import DiagnosticReport, DiagnosticResult, Subject, to_utc, utc_now

from .. import schema as v2_schema
from .evidence import EvidenceStage, PipelineEvidence

_TABLE_FAILURES = {
    "not_journaled": "The table is not journaled, so its changes cannot be captured.",
    "images_incomplete": "The table is journaled without before images (*BOTH), so updates and deletes are incomplete.",
    "journal_mismatch": "The selected tables use different journals; each journal needs its own reader.",
}
_STAGES = (
    ("capture.reading", "capture", "reading", "Journal capture"),
    ("delivery.snowflake", "delivery", "delivery", "Snowflake delivery"),
    ("reconciliation.destination", "reconciliation", "reconciliation", "Destination reconciliation"),
)
_MAX_REASON = 300


class DiagnosticsPipelineNotFoundError(LookupError):
    """Unknown pipeline, or a pipeline of another organization."""


class DiagnosticsService:
    def __init__(self, engine: Engine, *, org_id: str, version: str,
                 evidence_reader: Callable[[str], PipelineEvidence] | None) -> None:
        self._engine = engine
        self._org_id = org_id
        self._version = version
        self._evidence = evidence_reader

    def report(self, *, pipeline_id: str | None = None) -> DiagnosticReport:
        now = utc_now()
        sources, destinations, tables, pipelines = self._records()
        if pipeline_id is not None:
            pipelines = [row for row in pipelines if row["id"] == pipeline_id]
            if not pipelines:
                raise DiagnosticsPipelineNotFoundError(pipeline_id)
            table_ids = {row["table_id"] for row in pipelines}
            tables = [row for row in tables if row["id"] in table_ids]
            source_ids = {row["source_id"] for row in tables}
            sources = [row for row in sources if row["id"] in source_ids]
            destination_ids = {row["destination_id"] for row in pipelines}
            destinations = [row for row in destinations if row["id"] in destination_ids]
        results = [DiagnosticResult(
            code="control_plane.api", layer="control_plane", status="passed", required=True, observed_at=now,
            provenance="control-plane:api", subject=Subject("control_plane", "v2"),
            summary="The control plane API answered this request.",
            next_action="No action.",
            limits="Proves the API and its database answered; capture and delivery are separate results.",
        )]
        for row in sources:
            results.extend(self._source_results(row, now))
        for row in tables:
            results.append(self._table_result(row, now))
        for row in destinations:
            results.append(self._destination_result(row, now))
        table_labels = {row["id"]: f"{row['schema_name']}.{row['table_name']}" for row in tables}
        for row in pipelines:
            results.extend(self._pipeline_results(row, table_labels.get(row["table_id"]), now))
        return DiagnosticReport(results=tuple(results), generated_at=now, scope="control_plane", synthetic=False,
                                software={"product": "quadringent", "version": self._version,
                                          "distribution": "control-plane"})

    def sensitive_identifiers(self) -> list[tuple[str, str]]:
        """Values the export must replace wherever they appear in free text."""
        sources, destinations, _, _ = self._records()
        found: list[tuple[str, str]] = []
        for row in sources:
            found += [("host", row["ibmi_host"]), ("user", row["ibmi_user"]), ("source", row["display_name"])]
        for row in destinations:
            found += [("account", row["snowflake_account"])]
            found += [("user", value) for value in (row["service_user"], row["service_role"]) if value]
        return [(kind, value) for kind, value in found if value]

    def _records(self):
        sources_t, destinations_t = v2_schema.sources, v2_schema.destinations
        tables_t, pipelines_t = v2_schema.tables, v2_schema.pipelines
        with self._engine.connect() as connection:
            sources = connection.execute(
                select(sources_t).where(sources_t.c.org_id == self._org_id).order_by(sources_t.c.id)).mappings().all()
            destinations = connection.execute(
                select(destinations_t).where(destinations_t.c.org_id == self._org_id)
                .order_by(destinations_t.c.id)).mappings().all()
            tables = connection.execute(
                select(tables_t).select_from(tables_t.join(sources_t, tables_t.c.source_id == sources_t.c.id))
                .where(sources_t.c.org_id == self._org_id).order_by(tables_t.c.id)).mappings().all()
            pipelines = connection.execute(
                select(pipelines_t, tables_t.c.source_id)
                .select_from(pipelines_t.join(tables_t, pipelines_t.c.table_id == tables_t.c.id)
                             .join(sources_t, tables_t.c.source_id == sources_t.c.id))
                .where(sources_t.c.org_id == self._org_id).order_by(pipelines_t.c.id)).mappings().all()
        return list(sources), list(destinations), list(tables), list(pipelines)

    def _source_results(self, row, now: str) -> list[DiagnosticResult]:
        subject = Subject("source", row["id"], row["display_name"])
        observed = to_utc(row["updated_at"]) or now
        if row["detected_version"]:
            auth = DiagnosticResult(
                code="source.authentication", layer="authentication", status="passed", required=True,
                observed_at=observed, provenance="control-plane:source-probe", subject=subject,
                summary="A source probe signed on to IBM i and read its release.",
                next_action="Run Test again after any change to the IBM i account or password.",
                limits="Last successful probe recorded by the control plane; not re-tested by this diagnostic.",
            )
        else:
            auth = DiagnosticResult(
                code="source.authentication", layer="authentication", status="unknown", required=True,
                observed_at=now, provenance="control-plane:source-record", subject=subject,
                summary="No probe has confirmed IBM i sign-on; configured credentials are not proof of authentication.",
                next_action="Run Test for this source in the activation wizard.",
                limits="The control plane records detected values only after a successful probe.",
            )
        if row["tls_fingerprint"]:
            tls = DiagnosticResult(
                code="source.tls", layer="tls", status="passed", required=True, observed_at=observed,
                provenance="control-plane:source-probe", subject=subject,
                summary="A source probe completed a TLS handshake with the IBM i host.",
                next_action="Compare the fingerprint with the one shown by the IBM i administrator.",
                limits="Last successful probe recorded by the control plane; not re-tested by this diagnostic.",
                evidence={"sha256_fingerprint": str(row["tls_fingerprint"])[:120]},
            )
        else:
            tls = DiagnosticResult(
                code="source.tls", layer="tls", status="unknown", required=True, observed_at=now,
                provenance="control-plane:source-record", subject=subject,
                summary="No probe has confirmed TLS with the IBM i host.",
                next_action="Run Test for this source, or quadringent doctor --check-ibmi HOST --tls.",
                limits="No TLS observation is recorded for this source.",
            )
        return [auth, tls]

    def _table_result(self, row, now: str) -> DiagnosticResult:
        readiness = row["readiness"]
        subject = Subject("table", row["id"], f"{row['schema_name']}.{row['table_name']}")
        observed = to_utc(row["discovered_at"]) or now
        common = dict(code="source_prerequisites.table", layer="source_prerequisites", required=True,
                      observed_at=observed, provenance="control-plane:table-discovery", subject=subject,
                      evidence={"readiness": readiness or "unknown"})
        if readiness in _TABLE_FAILURES:
            return DiagnosticResult(
                status="failed", summary=_TABLE_FAILURES[readiness],
                next_action="Review the suggested CL commands with the IBM i administrator, run them, then refresh the table catalog.",
                limits="Readiness from the last catalog refresh; the IBM i catalog may have changed since.",
                **common)
        if readiness == "ready":
            return DiagnosticResult(
                status="passed", summary="The table is journaled with before and after images and has a key.",
                next_action="No action.",
                limits="Readiness from the last catalog refresh; the IBM i catalog may have changed since.", **common)
        if readiness == "no_key":
            return DiagnosticResult(
                status="unknown" if row["key_strategy"] != "rrn" else "passed",
                summary=("The table has no primary key or unique constraint."
                         if row["key_strategy"] != "rrn" else
                         "The table has no key; its physical position (RRN) was acknowledged as the key."),
                next_action=("Declare key columns, or acknowledge the RRN strategy and its reorganization risk."
                             if row["key_strategy"] != "rrn" else
                             "Plan a resynchronization after any RGZPFM or CLRPFM on this table."),
                limits="Readiness from the last catalog refresh.", **common)
        return DiagnosticResult(
            status="unknown", summary="The table has not been classified by a catalog refresh.",
            next_action="Refresh the table catalog for this source.", limits="No readiness is recorded.", **common)

    def _destination_result(self, row, now: str) -> DiagnosticResult:
        state = row["verification_state"]
        subject = Subject("destination", row["id"], row["snowflake_account"])
        status = {"verified": "passed", "failed": "failed"}.get(state, "unknown")
        summaries = {
            "passed": "The last access verification succeeded for the service role.",
            "failed": "The last access verification failed: the service role cannot use every declared object.",
            "unknown": "Snowflake access has not been verified.",
        }
        actions = {
            "passed": "Verify access again after changing roles or grants.",
            "failed": "Run the generated setup script with an administrator role, then Verify access again.",
            "unknown": "Run the generated setup script, then Verify access.",
        }
        return DiagnosticResult(
            code="destination.verification", layer="destination", status=status, required=True, observed_at=now,
            provenance="control-plane:destination-record", subject=subject, summary=summaries[status],
            next_action=actions[status],
            limits="Recorded verification state; the verification time is not recorded and this diagnostic does not connect to Snowflake.",
            evidence={"verification_state": state},
        )

    def _pipeline_results(self, row, table_label: str | None, now: str) -> list[DiagnosticResult]:
        subject = Subject("pipeline", row["id"], table_label)
        results: list[DiagnosticResult] = []
        state = row["declared_state"]
        reason = (row["attention_reason"] or "")[:_MAX_REASON]
        if state == "attention":
            results.append(DiagnosticResult(
                code="pipeline.state", layer="capture", status="failed", required=True,
                observed_at=to_utc(row["updated_at"]) or now, since=to_utc(row["updated_at"]),
                provenance="control-plane:pipeline-record", subject=subject,
                summary="The pipeline needs attention.",
                next_action="Open the table logs and the on-call runbook entry for this state before acting.",
                limits="Declared state only; it does not measure delivery.",
                evidence={"declared_state": state, "attention_reason": reason},
            ))
        else:
            results.append(DiagnosticResult(
                code="pipeline.state", layer="capture", status="passed", required=False, observed_at=now,
                provenance="control-plane:pipeline-record", subject=subject,
                summary=f"The pipeline is declared {state}.", next_action="No action.",
                limits="Declared state only; capture and delivery are separate results.",
                evidence={"declared_state": state},
            ))
        evidence = self._evidence(row["id"]) if self._evidence else PipelineEvidence(row["id"])
        stage_states = []
        for code, layer, attribute, label in _STAGES:
            stage: EvidenceStage = getattr(evidence, attribute)
            results.append(_stage_result(code, layer, label, stage, evidence, subject, now))
            stage_states.append(stage.state)
        live = evidence.evidence_kind == "live"
        if "stale" in stage_states:
            status, summary = "failed", "At least one evidence stage is stale; the current state is unknown."
        elif live and "observed" in stage_states:
            status, summary = "passed", "Observed evidence is current for this pipeline."
        else:
            status, summary = "unknown", "No current live evidence is available for this pipeline."
        results.append(DiagnosticResult(
            code="evidence.freshness", layer="evidence_freshness", status=status, required=True, observed_at=now,
            provenance=f"control-plane:evidence:{evidence.provenance}"[:120], subject=subject, summary=summary,
            next_action=("Check the reader and loader for this table, then wait for new evidence."
                         if status != "passed" else "No action."),
            limits="Freshness of recorded evidence only; it is not a reconciliation.",
            evidence={"evidence_kind": evidence.evidence_kind},
        ))
        return results


def _stage_result(code, layer, label, stage: EvidenceStage, evidence: PipelineEvidence, subject, now) -> DiagnosticResult:
    kind = evidence.evidence_kind
    common = dict(code=code, layer=layer, required=True, provenance=f"control-plane:evidence:{evidence.provenance}"[:120],
                  subject=subject, evidence={"stage_state": stage.state, "evidence_kind": kind,
                                             "event_count": stage.event_count})
    observed = to_utc(stage.observed_at)
    if stage.state == "stale":
        return DiagnosticResult(
            status="failed", observed_at=observed or now, since=observed,
            summary=f"{label} evidence is stale; nothing newer has been observed.",
            next_action="Open the table logs; check the reader and loader for this table before any action.",
            limits="The last observation time is shown; the current state is unknown.", **common)
    if stage.state == "observed" and kind == "live":
        return DiagnosticResult(
            status="passed", observed_at=observed or now,
            summary=f"{label} was observed for this table.", next_action="No action.",
            limits=f"Observed scope only ({stage.detail[:200]}).", **common)
    if stage.state == "observed":
        return DiagnosticResult(
            status="unknown", observed_at=observed or now,
            summary=f"{label} evidence is {('synthetic' if kind == 'simulation' else kind)}, not a current measurement.",
            next_action="Read live evidence from a running installation.",
            limits="Synthetic or historical evidence never proves the current state.", **common)
    return DiagnosticResult(
        status="unknown", observed_at=now, summary=f"{label} has not been measured for this table.",
        next_action="Wait for the reader and loader to report evidence; check the table logs if nothing arrives.",
        limits="Absent evidence is not a failure and not a success.", **common)
