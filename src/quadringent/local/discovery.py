"""Reading the probe and discovery job outputs, and deciding whether the table can be copied.

Both jobs run in the capture image and print one ``quadringent_*_result=`` JSON
line. This module never opens a connection: it only interprets that output.
The IBM i is never changed: when a table is not ready, the exact CL commands are
printed for the IBM i administrator.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from ..snowflake_destination import ColumnDefinition, IbmiColumnType, UnsupportedColumnTypeError
from ..table_discovery import (
    READY,
    NO_KEY,
    DiscoverProtocolError,
    DiscoveredColumn,
    DiscoveredTable,
    TableReadiness,
    classify_table,
    discovered_column_to_type_kwargs,
    parse_discover_output,
)

PROBE_PREFIX = "quadringent_probe_result="
DISCOVER_PREFIX = "quadringent_discover_result="


class JobOutputError(RuntimeError):
    """A job did not print the result line it should have printed."""


@dataclass(frozen=True)
class ProbeSummary:
    network_ok: bool
    network_detail: str
    tls_ok: bool
    tls_detail: str
    tls_trust: str | None
    tls_fingerprint: str | None
    auth_ok: bool
    auth_detail: str
    ibmi_version: str | None
    qtimzon: str | None
    detected_timezone: str | None

    @property
    def authentication_attempted(self) -> bool:
        return self.network_ok and self.tls_ok and self.tls_trust != "unknown"


def result_line(stdout: str, prefix: str) -> dict:
    """The JSON object of the last ``prefix``-line of a job output."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(prefix):
            try:
                document = json.loads(line[len(prefix):])
            except ValueError:
                break
            if isinstance(document, dict):
                return document
            break
    raise JobOutputError("the job printed no usable result line")


def parse_probe(stdout: str) -> ProbeSummary:
    document = result_line(stdout, PROBE_PREFIX)
    try:
        network, tls, authentication = document["network"], document["tls"], document["authentication"]
        return ProbeSummary(
            network_ok=bool(network["ok"]), network_detail=str(network["detail"]),
            tls_ok=bool(tls["ok"]), tls_detail=str(tls["detail"]),
            tls_trust=tls.get("trust"), tls_fingerprint=tls.get("fingerprint"),
            auth_ok=bool(authentication["ok"]), auth_detail=str(authentication["detail"]),
            ibmi_version=document.get("ibmi_version"), qtimzon=document.get("qtimzon"),
            detected_timezone=document.get("detected_timezone"),
        )
    except (KeyError, TypeError):
        raise JobOutputError("the probe result has an unexpected shape") from None


def parse_discovery(stdout: str) -> tuple[DiscoveredTable, ...]:
    document = result_line(stdout, DISCOVER_PREFIX)
    raw = document.get("raw_output")
    if not isinstance(raw, str):
        raise JobOutputError("the discovery result has an unexpected shape")
    # The worker output may end with its own protocol markers; keep only table rows.
    rows = "\n".join(line for line in raw.splitlines() if line.startswith("table\t"))
    try:
        return parse_discover_output(rows)
    except DiscoverProtocolError:
        raise JobOutputError("the discovery output does not follow the table protocol") from None


def find_table(tables: tuple[DiscoveredTable, ...], library: str, name: str) -> DiscoveredTable | None:
    library, name = library.upper(), name.upper()
    for table in tables:
        if table.library.upper() == library and name in (table.system_name.upper(), table.sql_name.upper()):
            return table
    return None


def declared_columns(columns: tuple[DiscoveredColumn, ...]) -> list[dict[str, object]] | None:
    """Same result as ``v2/services/tables.py::_auto_discovered_columns`` (parity test).

    One unrecognised or duplicated column gives up the whole table, never a partial declaration.
    """
    if not columns:
        return None
    declared: list[dict[str, object]] = []
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            return None
        seen.add(column.name)
        type_kwargs = discovered_column_to_type_kwargs(column)
        if type_kwargs is None:
            return None
        try:
            column_type = IbmiColumnType(**type_kwargs)
            ColumnDefinition(name=column.name, type=column_type, nullable=column.nullable)
        except (ValueError, UnsupportedColumnTypeError):
            return None
        declared.append(
            {
                "name": column.name,
                "kind": column_type.kind,
                "length": column_type.length,
                "precision": column_type.precision,
                "scale": column_type.scale,
                "timestamp_precision": column_type.timestamp_precision,
                "ccsid": column_type.ccsid,
                "nullable": column.nullable,
            }
        )
    return declared


@dataclass(frozen=True)
class TableAssessment:
    table: DiscoveredTable
    readiness: TableReadiness
    ready: bool
    reasons: tuple[str, ...]
    journal_library: str | None
    journal_name: str | None
    key_columns: tuple[str, ...]
    columns: tuple[dict[str, object], ...]

    def cl_commands(self) -> tuple[str, ...]:
        return tuple(command.command for command in self.readiness.cl_commands)


def assess_table(table: DiscoveredTable) -> TableAssessment:
    """Can this table be copied by local mode? One table, journaled *BOTH, with a primary or unique key."""
    readiness = classify_table(table)
    reasons: list[str] = []
    if readiness.state != READY:
        reasons.append(readiness.explanation)
        if readiness.state == NO_KEY:
            reasons.append(
                "Local mode copies only tables with a primary or unique key; tables identified by their "
                "physical position (RRN) are not supported."
            )
    columns = declared_columns(table.columns)
    if columns is None:
        reasons.append(
            "Some column types are not supported or the column list is empty, so the Snowflake tables "
            "cannot be declared."
        )
    if not (table.journal_library and table.journal_name):
        reasons.append("The journal of the table is unknown.")
    return TableAssessment(
        table=table, readiness=readiness, ready=not reasons,
        reasons=tuple(reasons), journal_library=table.journal_library, journal_name=table.journal_name,
        key_columns=table.key_columns, columns=tuple(columns or ()),
    )
