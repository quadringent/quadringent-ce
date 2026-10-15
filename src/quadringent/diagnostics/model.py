"""Versioned diagnostic contract ``quadringent.diagnostic.v1``.

The overall verdict is computed from the results and never read from an
input document: a failed result blocks, an unknown or skipped required result
leaves the report incomplete, and nothing is ever reported as "healthy".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Mapping

SCHEMA = "quadringent.diagnostic.v1"

LAYERS = (
    "local_tools",
    "local_config",
    "network",
    "tls",
    "authentication",
    "source_prerequisites",
    "destination",
    "control_plane",
    "capture",
    "delivery",
    "reconciliation",
    "evidence_freshness",
)
STATUSES = ("passed", "failed", "unknown", "skipped")
SCOPES = ("local", "control_plane")
SUBJECT_KINDS = ("source", "destination", "pipeline", "table", "host", "tool", "control_plane")

# Exit codes of ``quadringent doctor`` (documented in docs/product/operator-guide.md).
EXIT_READY = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

_CODE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
_MAX_TEXT = 500
_MAX_EVIDENCE_FIELDS = 20
_MAX_RESULTS = 500


class DiagnosticContractError(ValueError):
    """A document or value does not follow quadringent.diagnostic.v1."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_utc(value: object) -> str | None:
    """Normalize a datetime or ISO string to the contract's UTC form."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise DiagnosticContractError(f"invalid timestamp: {value!r}") from error
    if not isinstance(value, datetime):
        raise DiagnosticContractError("timestamps must be datetimes or ISO strings")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(name: str, value: object, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DiagnosticContractError(f"{name} must be a non-empty string of at most {limit} characters")
    return value


def _timestamp(name: str, value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        raise DiagnosticContractError(f"{name} must be a UTC timestamp ending with Z")
    return value


@dataclass(frozen=True)
class Subject:
    kind: str
    ref: str
    label: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SUBJECT_KINDS:
            raise DiagnosticContractError(f"unknown subject kind: {self.kind!r}")
        _text("subject.ref", self.ref, limit=200)
        if self.label is not None:
            _text("subject.label", self.label, limit=200)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "ref": self.ref, "label": self.label}


@dataclass(frozen=True)
class DiagnosticResult:
    code: str
    layer: str
    status: str
    required: bool
    observed_at: str
    provenance: str
    summary: str
    next_action: str
    limits: str
    subject: Subject | None = None
    since: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CODE.fullmatch(self.code) is None:
            raise DiagnosticContractError(f"invalid code: {self.code!r}")
        if self.layer not in LAYERS:
            raise DiagnosticContractError(f"unknown layer: {self.layer!r}")
        if self.status not in STATUSES:
            raise DiagnosticContractError(f"unknown status: {self.status!r}")
        if not isinstance(self.required, bool):
            raise DiagnosticContractError("required must be a boolean")
        _timestamp("observed_at", self.observed_at)
        _timestamp("since", self.since, optional=True)
        for name in ("provenance", "summary", "next_action", "limits"):
            _text(name, getattr(self, name))
        if not isinstance(self.evidence, Mapping) or len(self.evidence) > _MAX_EVIDENCE_FIELDS:
            raise DiagnosticContractError("evidence must be a small flat object")
        for key, value in self.evidence.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
                raise DiagnosticContractError(f"invalid evidence key: {key!r}")
            if isinstance(value, str):
                if len(value) > _MAX_TEXT:
                    raise DiagnosticContractError(f"evidence {key} is too long")
            elif value is not None and not isinstance(value, (bool, int, float)):
                raise DiagnosticContractError(f"evidence {key} must be a scalar")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "layer": self.layer,
            "status": self.status,
            "required": self.required,
            "observed_at": self.observed_at,
            "provenance": self.provenance,
            "subject": self.subject.to_dict() if self.subject else None,
            "since": self.since,
            "summary": self.summary,
            "next_action": self.next_action,
            "limits": self.limits,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class DiagnosticReport:
    results: tuple[DiagnosticResult, ...]
    generated_at: str
    scope: str
    synthetic: bool
    software: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise DiagnosticContractError(f"unknown scope: {self.scope!r}")
        if len(self.results) > _MAX_RESULTS:
            raise DiagnosticContractError("too many results")
        _timestamp("generated_at", self.generated_at)

    def overall(self) -> dict[str, object]:
        counts = {status: sum(1 for item in self.results if item.status == status) for status in STATUSES}
        if counts["failed"]:
            status = "blocked"
            summary = f"{counts['failed']} check(s) failed; fix them before relying on this installation."
        elif not self.results or any(item.required and item.status in ("unknown", "skipped") for item in self.results):
            status = "incomplete"
            summary = "Some required checks were not observed; their state is unknown."
        else:
            status = "ready"
            summary = "Every required check passed in this observation."
        if self.synthetic:
            summary += " Synthetic data: not a measurement of a real installation."
        return {"status": status, **counts, "summary": summary}

    def exit_code(self) -> int:
        return {"ready": EXIT_READY, "blocked": EXIT_BLOCKED, "incomplete": EXIT_INCOMPLETE}[self.overall()["status"]]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "generated_at": self.generated_at,
            "scope": self.scope,
            "synthetic": self.synthetic,
            "software": dict(self.software),
            "overall": self.overall(),
            "results": [item.to_dict() for item in self.results],
        }


def _subject(value: object) -> Subject | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DiagnosticContractError("subject must be an object or null")
    return Subject(kind=value.get("kind"), ref=value.get("ref"), label=value.get("label"))


def parse_report(document: object) -> DiagnosticReport:
    """Parse a v1 document; unknown additive fields are ignored, the overall
    verdict is recomputed."""
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        raise DiagnosticContractError(f"expected schema {SCHEMA}")
    raw_results = document.get("results")
    if not isinstance(raw_results, list):
        raise DiagnosticContractError("results must be a list")
    results = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise DiagnosticContractError("each result must be an object")
        results.append(DiagnosticResult(
            code=raw.get("code"), layer=raw.get("layer"), status=raw.get("status"), required=raw.get("required"),
            observed_at=raw.get("observed_at"), provenance=raw.get("provenance"), summary=raw.get("summary"),
            next_action=raw.get("next_action"), limits=raw.get("limits"), subject=_subject(raw.get("subject")),
            since=raw.get("since"), evidence=raw.get("evidence") or {},
        ))
    software = document.get("software")
    if not isinstance(software, Mapping) or not all(isinstance(v, str) for v in software.values()):
        raise DiagnosticContractError("software must be an object of strings")
    synthetic = document.get("synthetic")
    if not isinstance(synthetic, bool):
        raise DiagnosticContractError("synthetic must be a boolean")
    return DiagnosticReport(results=tuple(results), generated_at=document.get("generated_at"),
                            scope=document.get("scope"), synthetic=synthetic, software=dict(software))
