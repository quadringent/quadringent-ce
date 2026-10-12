"""Shareable diagnostic export, built only from allowed fields.

Nothing is archived from the working directory, the database, environment
variables or raw logs. Identifiers are replaced by pseudonyms that stay
consistent within one export, free text is scrubbed of secret-shaped values,
evidence keeps only allowed scalar fields, and every omission is listed with
its reason. Nothing is ever sent anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile
from typing import Iterable

from .model import DiagnosticReport

EXPORT_SCHEMA = "quadringent.diagnostic-export.v1"
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 4_000_000
DEFAULT_MAX_TIMELINE = 200

# Evidence fields that may leave the installation, each with its allowed values.
_NUMBER = (int, float)
_ERRORS = frozenset({
    "host name not resolved", "timed out", "connection refused", "connection reset", "network error",
    "connection failed", "certificate does not match the host name", "certificate expired",
    "certificate not trusted", "TLS handshake refused", "CA file unreadable", "contract mismatch",
})
_ENUMS = {
    "stage_state": frozenset({"observed", "stale", "absent"}),
    "evidence_kind": frozenset({"live", "historical", "simulation", "absent"}),
    "readiness": frozenset({"ready", "not_journaled", "images_incomplete", "no_key", "journal_mismatch", "unknown"}),
    "declared_state": frozenset({"not_started", "copying", "live", "paused", "attention", "stopped", "removed"}),
    "verification_state": frozenset({"verified", "failed", "declared_not_verified"}),
    "images": frozenset({"*BOTH", "*AFTER"}),
    "key_strategy": frozenset({"primary_key", "unique", "rrn", "declared", "none"}),
    "tool": frozenset({"terraform", "helm", "kubectl", "aws", "session-manager-plugin", "gcloud"}),
}
_BOOLEANS = frozenset({"found", "custom_ca"})
_NUMBERS = frozenset({"port", "event_count", "lag_seconds", "duplicate_count", "status_code"})
_SOFTWARE_DISTRIBUTIONS = frozenset({"wheel", "installed", "checkout", "control-plane"})
# Provenance values come from the producers; anything else is replaced.
_PROVENANCES = frozenset({
    "control-plane:api", "control-plane:destination-record", "control-plane:pipeline-record",
    "control-plane:records", "control-plane:source-record", "control-plane:table-discovery",
    "doctor:control-plane", "doctor:not-requested", "doctor:not-tested", "local:PATH", "local:ca-file",
    "local:environment", "probe:tcp", "probe:tls",
})
_EVIDENCE_PROVENANCE = re.compile(r"control-plane:evidence:(?:absent|unreadable|demo_fixture|lab_fixture|"
                                  r"kubernetes_loader_log|synthetic_fixture|[a-z]+_(?:log|fixture|projection|store))")
_PSEUDONYMIZED_KINDS = {"host": "host", "source": "source", "destination": "destination", "pipeline": "pipeline",
                        "table": "table"}
_LABEL_KIND = {"destination": "account", "source": "source", "pipeline": "pipeline", "table": "table"}

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "[redacted: key material]"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "[redacted: URL]"),
    (re.compile(r"#/wizard/activate\S*"), "[redacted: activation link]"),
    (re.compile(r"\bBearer\s+\S+", re.I), "Bearer [redacted]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[redacted: token]"),
    (re.compile(r"\bqdt_[a-z]{2}_\w+"), "[redacted: token]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,})"),
     "[redacted: token]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}\b"), "[redacted: access key]"),
    (re.compile(r"\b([\w-]*(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|cookie|session|"
                r"authorization|credential|signature|sig)[\w-]*)[\"']?\s*[=:]\s*[\"']?[^\s;,&\"']+", re.I),
     r"\1=[redacted]"),
    (re.compile(r"\b(password|passwd|secret|token|api key)\s+(?:is|was)\s+\S+", re.I), r"\1 [redacted]"),
    (re.compile(r"\b(account|user|username|login|host|warehouse|role)\s*=\s*[^\s;,&\"']+", re.I), r"\1=[redacted]"),
    (re.compile(r"\b(account|user|username|login|warehouse|role)\s+(?=[A-Za-z0-9_.-]*[0-9_.])[A-Za-z0-9_.-]{3,}", re.I),
     r"\1 [redacted]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[redacted: email]"),
    (re.compile(r"(?:~|/(?:Users|home|private|var|etc|tmp|opt|root|srv|mnt))(?:/[^\s'\"]+)+"), "[redacted: path]"),
    (re.compile(r"\b[A-Za-z]:\\\S+"), "[redacted: path]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[redacted: address]"),
    (re.compile(r"\b[0-9a-f]*::[0-9a-f:]+\b", re.I), "[redacted: address]"),
    (re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b(?:/\S*)?", re.I), "[redacted: host]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[redacted: hex]"),
    (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), "[redacted: encoded value]"),
)


class ExportPathError(ValueError):
    """The archive destination is unsafe; nothing was written."""


class ExportTooLargeError(ValueError):
    """The export exceeds its bounded size."""


@dataclass(frozen=True)
class ExportBundle:
    files: dict[str, bytes]
    preview: dict[str, object]
    generated_at: str

    def to_document(self) -> dict[str, object]:
        """The JSON form served to the cockpit: preview first, then text files."""
        return {
            "schema": EXPORT_SCHEMA,
            "generated_at": self.generated_at,
            "preview": self.preview,
            "files": {name: content.decode("utf-8") for name, content in self.files.items()},
        }


class _Pseudonyms:
    def __init__(self, keep: bool) -> None:
        self.keep = keep
        self.mapping: dict[tuple[str, str], str] = {}
        self.originals: dict[str, str] = {}

    def __call__(self, kind: str, value: str) -> str:
        if self.keep:
            return value
        key = (kind, value)
        if key not in self.mapping:
            count = sum(1 for known_kind, _ in self.mapping if known_kind == kind) + 1
            self.mapping[key] = f"{kind}-{count}"
            self.originals[value] = self.mapping[key]
        return self.mapping[key]

    def counts(self) -> dict[str, int]:
        result = {"hosts": 0, "accounts": 0, "identifiers": 0}
        for kind, _ in self.mapping:
            if kind == "host":
                result["hosts"] += 1
            elif kind in ("account", "user"):
                result["accounts"] += 1
            else:
                result["identifiers"] += 1
        return result


def _scrub(text: str, pseudonyms: _Pseudonyms, omitted: list[dict[str, str]], field: str) -> str:
    cleaned = text
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if cleaned != text:
        omitted.append({"field": field, "reason": "secret-shaped value removed"})
    if not pseudonyms.keep:
        replaced = cleaned
        for original in sorted(pseudonyms.originals, key=len, reverse=True):
            replaced = re.sub(re.escape(original), pseudonyms.originals[original], replaced, flags=re.I)
        if replaced != cleaned:
            omitted.append({"field": field, "reason": "identifier pseudonymized by default"})
        cleaned = replaced
    return cleaned


def build_export(
    report: DiagnosticReport,
    *,
    keep_identifiers: bool = False,
    extra_identifiers: Iterable[tuple[str, str]] = (),
    max_timeline: int = DEFAULT_MAX_TIMELINE,
) -> ExportBundle:
    """Build the export from the report's allowed fields.

    ``extra_identifiers`` lists (kind, value) pairs the producer knows are
    sensitive (IBM i user, Snowflake service user...), so they are replaced
    wherever they appear in free text.
    """
    pseudonyms = _Pseudonyms(keep_identifiers)
    omitted: list[dict[str, str]] = []
    document = report.to_dict()
    pending_labels: list[dict] = []
    software = {}
    for key, value in document["software"].items():
        if key == "product" and value == "quadringent":
            software[key] = value
        elif key == "version" and re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", value):
            software[key] = value
        elif key == "distribution" and value in _SOFTWARE_DISTRIBUTIONS:
            software[key] = value
        else:
            omitted.append({"field": f"software.{key}", "reason": "not an exported field"})
    document["software"] = software

    for item in document["results"]:
        subject = item.get("subject")
        if subject and subject["kind"] in _PSEUDONYMIZED_KINDS:
            subject["ref"] = pseudonyms(_PSEUDONYMIZED_KINDS[subject["kind"]], subject["ref"])
            if not keep_identifiers:
                omitted.append({"field": f"{item['code']}.subject.ref", "reason": "identifier pseudonymized by default"})
            if subject.get("label") and subject["kind"] in _LABEL_KIND:
                label_kind = _LABEL_KIND[subject["kind"]]
                subject["label"] = (subject["ref"] if label_kind == _PSEUDONYMIZED_KINDS[subject["kind"]]
                                    else pseudonyms(label_kind, subject["label"]))
                if not keep_identifiers:
                    omitted.append({"field": f"{item['code']}.subject.label",
                                    "reason": "identifier pseudonymized by default"})
            elif subject.get("label"):
                pending_labels.append(item)
    for kind, value in extra_identifiers:
        if value:
            pseudonyms(kind, value)

    for item in pending_labels:
        item["subject"]["label"] = _scrub(item["subject"]["label"], pseudonyms, omitted, f"{item['code']}.subject.label")
    for item in document["results"]:
        subject = item.get("subject")
        if subject and subject["kind"] not in _PSEUDONYMIZED_KINDS:
            subject["ref"] = _scrub(subject["ref"], pseudonyms, omitted, f"{item['code']}.subject.ref")
    for item in document["results"]:
        code = item["code"]
        for name in ("summary", "next_action", "limits"):
            item[name] = _scrub(item[name], pseudonyms, omitted, f"{code}.{name}")
        if item["provenance"] not in _PROVENANCES and _EVIDENCE_PROVENANCE.fullmatch(item["provenance"]) is None:
            item["provenance"] = "other"
            omitted.append({"field": f"{code}.provenance", "reason": "not a known provenance value"})
        kept: dict[str, object] = {}
        for key, value in item["evidence"].items():
            if _allowed_evidence(key, value):
                kept[key] = value
            else:
                omitted.append({"field": f"{code}.evidence.{key}",
                                "reason": "not an exported field or value (free text may hold secrets)"})
        item["evidence"] = kept

    events = sorted(
        ({"at": item["since"] or item["observed_at"], "code": item["code"], "status": item["status"],
          "subject": item["subject"]["ref"] if item.get("subject") else None} for item in document["results"]),
        key=lambda event: (event["at"], event["code"]),
    )
    timeline = {"events": events[:max_timeline], "truncated": len(events) > max_timeline, "limit": max_timeline}

    limits = [
        "Built from diagnostic results only: no database, environment, log file or table row is included.",
        "Identifiers are pseudonymized by default; pseudonyms are consistent within this export only.",
        "Secret-shaped values and free-text evidence are removed; see omitted.",
    ]
    if document["synthetic"]:
        limits.append("Synthetic data: not a measurement of a real installation.")
    files = {
        "report.json": _json(document),
        "timeline.json": _json(timeline),
        "README.txt": (
            "Quadringent diagnostic export\n\n"
            f"Generated (UTC): {report.generated_at}\n"
            f"Overall: {document['overall']['status']}\n\n"
            + "\n".join(f"- {line}" for line in limits) + "\n"
        ).encode("utf-8"),
    }
    manifest = {
        "schema": EXPORT_SCHEMA,
        "generated_at": report.generated_at,
        "software": document["software"],
        "synthetic": document["synthetic"],
        "files": [{"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in sorted(files.items())],
        "pseudonymized": pseudonyms.counts(),
        "omitted": omitted,
        "limits": limits,
    }
    files["manifest.json"] = _json(manifest)
    for name, content in files.items():
        if len(content) > MAX_FILE_BYTES:
            raise ExportTooLargeError(f"{name} exceeds {MAX_FILE_BYTES} bytes")
    if sum(len(content) for content in files.values()) > MAX_TOTAL_BYTES:
        raise ExportTooLargeError("export exceeds its total size limit")
    preview = {
        "files": [{"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in sorted(files.items())],
        "omitted": omitted,
        "pseudonymized": pseudonyms.counts(),
        "limits": limits,
    }
    return ExportBundle(files=files, preview=preview, generated_at=report.generated_at)


def _allowed_evidence(key: str, value: object) -> bool:
    if value is None:
        return key in _NUMBERS or key in _ENUMS
    if key in _BOOLEANS:
        return isinstance(value, bool)
    if key in _NUMBERS:
        return isinstance(value, _NUMBER) and not isinstance(value, bool)
    if key == "error":
        return isinstance(value, str) and (value in _ERRORS or re.fullmatch(r"status \d{3}", value) is not None)
    if key == "sha256_fingerprint":
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    return key in _ENUMS and value in _ENUMS[key]


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def write_archive(bundle: ExportBundle, destination: Path) -> Path:
    """Write a private .tar.gz; refuse existing files, links and missing folders."""
    destination = Path(destination)
    if not destination.name.endswith(".tar.gz") or destination.name.startswith("."):
        raise ExportPathError("the export must be a .tar.gz file name")
    parent = destination.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError as error:
        raise ExportPathError(f"folder {parent} does not exist") from error
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ExportPathError(f"{parent} must be a real folder, not a link")
    if os.path.lexists(destination):
        raise ExportPathError(f"{destination} already exists; choose a new file name")
    stamp = datetime.fromisoformat(bundle.generated_at.replace("Z", "+00:00"))
    prefix = "quadringent-diagnostic-" + stamp.strftime("%Y%m%dT%H%M%SZ")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=int(stamp.timestamp())) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(bundle.files):
                content = bundle.files[name]
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size, info.mode, info.mtime = len(content), 0o600, int(stamp.timestamp())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(content))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as error:
        raise ExportPathError(f"{destination} already exists; choose a new file name") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(buffer.getvalue())
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)
    return destination
