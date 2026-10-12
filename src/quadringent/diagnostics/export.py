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

# Evidence fields that may leave the installation, and the shape of their values.
ALLOWED_EVIDENCE = frozenset({
    "port", "error", "stage_state", "evidence_kind", "sha256_fingerprint", "custom_ca", "event_count",
    "lag_seconds", "duplicate_count", "readiness", "declared_state", "verification_state", "journal_status",
    "images", "key_strategy", "tool", "found", "status_code",
})
_SAFE_VALUE = re.compile(r"[A-Za-z0-9 _.:/()*#-]{0,120}")
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
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}\b"), "[redacted: access key]"),
    (re.compile(r"\b([\w-]*(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|cookie|session|"
                r"authorization|credential|signature|sig))[\w-]*\s*[=:]\s*[^\s;,&\"']+", re.I), r"\1=[redacted]"),
    (re.compile(r"\b(account|user|username|login|host|warehouse|role)\s*=\s*[^\s;,&\"']+", re.I), r"\1=[redacted]"),
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
            replaced = replaced.replace(original, pseudonyms.originals[original])
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
    for kind, value in extra_identifiers:
        if value:
            pseudonyms(kind, value)

    for item in document["results"]:
        code = item["code"]
        for name in ("summary", "next_action", "limits", "provenance"):
            item[name] = _scrub(item[name], pseudonyms, omitted, f"{code}.{name}")
        kept: dict[str, object] = {}
        for key, value in item["evidence"].items():
            if key not in ALLOWED_EVIDENCE:
                omitted.append({"field": f"{code}.evidence.{key}", "reason": "not an exported field (free text may hold secrets)"})
                continue
            if isinstance(value, str):
                value = _scrub(value, pseudonyms, omitted, f"{code}.evidence.{key}")
                if _SAFE_VALUE.fullmatch(value) is None:
                    omitted.append({"field": f"{code}.evidence.{key}", "reason": "value removed: secret-shaped or free text"})
                    continue
            kept[key] = value
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
