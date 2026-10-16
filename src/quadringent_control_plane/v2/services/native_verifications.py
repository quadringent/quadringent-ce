"""SQL-owned proof attempts; native collection/admission is unavailable by default.

Only an injected internal controller may supply an independently admitted result.
No endpoint or job-supplied JSON is accepted by this service.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Protocol
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select, update

from quadringent.qualification.verification_contract import DATASETS, VerificationScope
from quadringent.qualification.verification_stream import _timestamp
from .native_context import NativeContextConflict, canonical, context_snapshot, context_transaction
from .. import schema

_SHA = re.compile(r"[0-9a-f]{64}")
_PREPARATION = {"copy_intent", "copy_evidence", "registry", "binding", "checkpoint"}
_PROOFS = {"platform", "source", "raw_inventory", "native_format", "receiver_chain", "destination", "stable_cut"}
_REASONS = {"preparation_unavailable", "preparation_failed", "admission_unavailable",
            "admission_failed", "collection_failed", "controller_failed", "unsupported_native_format",
            "native_authority_unavailable"}


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("native_time_requires_timezone")
    return value.astimezone(timezone.utc)


def _sql_time(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else _utc(value)


def _text(value):
    if type(value) is not str or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_native_identifier")
    return value


@dataclass(frozen=True)
class NativeArtifact:
    name: str
    uri: str
    sha256: str
    version: str
    byte_count: int

    def __post_init__(self):
        _text(self.name)
        _text(self.version)
        parsed = urlsplit(self.uri)
        if (type(self.uri) is not str or len(self.uri) > 2048 or parsed.scheme not in {"s3", "gcs", "local"}
                or not parsed.netloc or parsed.username is not None or parsed.query or parsed.fragment
                or not parsed.path or ".." in parsed.path.split("/")
                or _SHA.fullmatch(self.sha256) is None or type(self.byte_count) is not int
                or not 1 <= self.byte_count <= 16 * 1024**3):
            raise ValueError("invalid_native_artifact")

    def to_dict(self):
        return dict(name=self.name, uri=self.uri, sha256=self.sha256,
                    version=self.version, byte_count=self.byte_count)


def _refs(artifacts, required):
    if (type(artifacts) is not tuple or len(artifacts) > 1000
            or any(type(ref) is not NativeArtifact for ref in artifacts)):
        raise ValueError("invalid_native_references")
    names = [ref.name for ref in artifacts]
    if len(set(names)) != len(names) or not required.issubset(names):
        raise ValueError("missing_native_references")
    return [ref.to_dict() for ref in artifacts]


@dataclass(frozen=True)
class NativePreparation:
    scope: VerificationScope
    artifacts: tuple[NativeArtifact, ...]


@dataclass(frozen=True)
class NativeAdmission:
    comparison: dict
    artifacts: tuple[NativeArtifact, ...]
    policy_revision: str


class NativePreparationPort(Protocol):
    def prepare(self, context: dict) -> NativePreparation: ...


class NativeAdmissionPort(Protocol):
    """Trusted controller-owned collection and independent native admission."""

    def admit(self, prepared: NativePreparation) -> NativeAdmission: ...


@dataclass(frozen=True)
class ControllerLease:
    request_id: str
    holder: str
    lease_generation: int
    revision: int


def _comparison(report, artifacts, scope, now):
    if (type(report) is not dict or report.get("format_version") != "quadringent-native-table-comparison-v1"
            or report.get("scope_sha256") != scope.sha256 or report.get("state") not in {"verified", "divergent"}
            or set(report.get("datasets", {})) != set(DATASETS) or report.get("failure_codes") != {}
            or report.get("equality_scope") != "complete_layer_contents_and_reconstructed_final_state"
            or report.get("update_pairing_verified") is not False
            or report.get("transaction_consistency_verified") is not False or type(report.get("ignored_fields")) is not int
            or report["ignored_fields"] != 0):
        raise ValueError("invalid_native_comparison")
    refs = {ref.name: ref for ref in artifacts}
    summary_fields = {"row_count", "byte_count", "sha256", "rows_sha256", "started_at", "completed_at",
                      "unique_count", "duplicate_count", "conflicting_identity_count", "exact_replay_count"}
    for name in DATASETS:
        summary = report["datasets"][name]
        if type(summary) is not dict or set(summary) != summary_fields:
            raise ValueError("incomplete_native_stream")
        for key in ("row_count", "unique_count", "duplicate_count", "conflicting_identity_count", "exact_replay_count"):
            if type(summary[key]) is not int or not 0 <= summary[key] <= 20_000_000:
                raise ValueError("invalid_native_stream_counts")
        if (summary["unique_count"] + summary["duplicate_count"] != summary["row_count"]
                or max(summary["conflicting_identity_count"], summary["exact_replay_count"]) > summary["duplicate_count"]
                or type(summary["byte_count"]) is not int or not 1 <= summary["byte_count"] <= 16 * 1024**3
                or _SHA.fullmatch(summary["sha256"]) is None or _SHA.fullmatch(summary["rows_sha256"]) is None
                or refs["stream:" + name].sha256 != summary["sha256"]
                or refs["stream:" + name].byte_count != summary["byte_count"]
                or _timestamp(summary["completed_at"]) > _timestamp(_utc(now).isoformat())
                or _timestamp(summary["completed_at"]) < _timestamp(summary["started_at"])):
            raise ValueError("native_stream_reference_mismatch")
    checks = report.get("checks")
    keys = {"snapshot_rows_complete", "journal_row_images_reconstructed", "all_streams_complete",
            "all_fields_compared", "no_logical_duplicates"}
    if (type(checks) is not dict or set(checks) != keys or any(type(v) is not bool for v in checks.values())
            or not checks["all_streams_complete"] or not checks["all_fields_compared"]
            or checks["snapshot_rows_complete"] != (
                report["datasets"]["snapshot"]["unique_count"] == scope.expected_snapshot_rows)
            or checks["no_logical_duplicates"] != all(
                s["conflicting_identity_count"] == 0 if name == "raw" else s["duplicate_count"] == 0
                for name, s in report["datasets"].items())):
        raise ValueError("invalid_native_checks")
    comparisons = report.get("comparisons")
    if type(comparisons) is not dict or set(comparisons) != {"journal_positions", "raw", "history", "source", "mirror"}:
        raise ValueError("incomplete_native_comparisons")
    count_keys = {"expected_count", "actual_count", "missing_count", "unexpected_count", "different_count"}
    for item in comparisons.values():
        if (type(item) is not dict or set(item) != count_keys | {"equal"}
                or any(type(item[k]) is not int or not 0 <= item[k] <= 40_000_000 for k in count_keys)
                or type(item["equal"]) is not bool
                or item["equal"] != (item["missing_count"] == item["unexpected_count"] == item["different_count"] == 0)
                or item["expected_count"] - item["missing_count"] != item["actual_count"] - item["unexpected_count"]
                or item["different_count"] > item["expected_count"] - item["missing_count"]):
            raise ValueError("invalid_native_comparison_counts")
    verified = all(checks.values()) and all(v["equal"] for v in comparisons.values())
    if report["state"] != ("verified" if verified else "divergent"):
        raise ValueError("native_report_state_mismatch")


class NativeVerificationsService:
    def __init__(self, engine, *, org_id, source_restriction=(), preparer=None, controller=None,
                 now=lambda: datetime.now(timezone.utc)):
        self.engine, self.org_id = engine, org_id
        self.source_restriction = tuple(source_restriction)
        self.preparer, self.controller, self.now = preparer, controller, now

    def _row(self, c, request_id):
        row = c.execute(select(schema.native_verifications).where(
            schema.native_verifications.c.id == request_id,
            schema.native_verifications.c.org_id == self.org_id,
        )).mappings().first()
        if row is None or self.source_restriction and row["source_id"] not in self.source_restriction:
            raise LookupError("native_verification_not_found")
        return row

    def _context(self, c, row, *, require_current=True):
        p, value, encoded, digest = context_snapshot(c, self.org_id, row["pipeline_id"], self.source_restriction)
        if require_current and (
            p["native_context_operation_id"] is not None or p["latest_native_verification_id"] != row["id"]
            or p["native_context_generation"] != row["context_generation"]
            or digest != row["context_sha256"]
        ):
            raise NativeContextConflict("native_context_superseded")
        return p, value, encoded, digest

    def _lease(self, row, lease):
        if (type(lease) is not ControllerLease or row["revision"] != lease.revision
                or row["controller_holder"] != lease.holder
                or row["lease_generation"] != lease.lease_generation
                or row["lease_expires_at"] is None
                or _sql_time(row["lease_expires_at"]) <= _utc(self.now())
                or row["lifecycle"] == "finished"):
            raise NativeContextConflict("native_controller_fence_lost")

    def request(self, pipeline_id, *, requested_by, idempotency_key):
        _text(requested_by)
        _text(idempotency_key)
        moment = _utc(self.now())
        with context_transaction(self.engine, self.org_id) as c:
            p, context, encoded, digest = context_snapshot(
                c, self.org_id, pipeline_id, self.source_restriction)
            if not p["active_run_id"] or p["native_context_operation_id"] is not None:
                raise NativeContextConflict("native_context_unavailable")
            existing = c.execute(select(schema.native_verifications).where(
                schema.native_verifications.c.org_id == self.org_id,
                schema.native_verifications.c.requested_by == requested_by,
                schema.native_verifications.c.idempotency_key == idempotency_key,
            )).mappings().first()
            if existing is not None:
                if existing["pipeline_id"] != pipeline_id or existing["context_sha256"] != digest:
                    raise NativeContextConflict("native_idempotency_conflict")
                request_id = existing["id"]
            else:
                request_id = uuid.uuid4().hex
                c.execute(schema.native_verifications.insert(), dict(
                    id=request_id, org_id=self.org_id, pipeline_id=pipeline_id,
                    table_id=p["table_id"], source_id=p["source_id"], destination_id=p["destination_id"],
                    copy_run_id=p["active_run_id"], context_generation=p["native_context_generation"],
                    context_json=encoded, context_sha256=digest, requested_by=requested_by,
                    idempotency_key=idempotency_key, created_at=moment, lifecycle="preparing",
                ))
                c.execute(update(schema.pipelines).where(schema.pipelines.c.id == pipeline_id).values(
                    latest_native_verification_id=request_id))
        return self.get(request_id)

    def claim(self, request_id, *, holder, lease_seconds=300):
        _text(holder)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 7200:
            raise ValueError("invalid_native_lease")
        moment = _utc(self.now())
        with context_transaction(self.engine, self.org_id) as c:
            row = self._row(c, request_id)
            self._context(c, row)
            if (row["lifecycle"] == "finished" or row["lease_expires_at"] is not None
                    and _sql_time(row["lease_expires_at"]) > moment):
                raise NativeContextConflict("native_controller_lease_busy")
            revision, generation = row["revision"] + 1, row["lease_generation"] + 1
            c.execute(update(schema.native_verifications).where(
                schema.native_verifications.c.id == request_id).values(
                    controller_holder=holder, lease_generation=generation, revision=revision,
                    lease_expires_at=moment + timedelta(seconds=lease_seconds)))
        return ControllerLease(request_id, holder, generation, revision)

    def _read_owned(self, lease):
        with context_transaction(self.engine, self.org_id) as c:
            row = self._row(c, lease.request_id)
            self._lease(row, lease)
            self._context(c, row)
            return dict(row)

    def renew(self, lease, *, lease_seconds=300):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 7200:
            raise ValueError("invalid_native_lease")
        with context_transaction(self.engine, self.org_id) as c:
            row = self._row(c, lease.request_id)
            self._lease(row, lease)
            self._context(c, row)
            c.execute(update(schema.native_verifications).where(
                schema.native_verifications.c.id == lease.request_id).values(
                    lease_expires_at=_utc(self.now()) + timedelta(seconds=lease_seconds)))
        return lease

    def prepare(self, lease):
        row = self._read_owned(lease)
        if not json.loads(row["context_json"])["organization"]["native_context_enforced"]:
            self.close_incomplete(lease, reason_code="native_authority_unavailable")
            return None
        if row["scope_json"] is not None:
            return lease
        if self.preparer is None:
            self.close_incomplete(lease, reason_code="preparation_unavailable")
            return None
        try:
            prepared = self.preparer.prepare(json.loads(row["context_json"]))
            if type(prepared) is not NativePreparation or type(prepared.scope) is not VerificationScope:
                raise ValueError("invalid_native_preparation")
            scope, context = prepared.scope, json.loads(row["context_json"])
            if ((scope.pipeline_id, scope.table_id, scope.copy_run_id, scope.source_library, scope.source_table,
                 scope.destination_database, scope.destination_schema)
                    != (row["pipeline_id"], row["table_id"], row["copy_run_id"],
                        context["table"]["schema_name"], context["table"]["table_name"],
                        context["destination"]["destination_database"], context["destination"]["destination_schema"])):
                raise ValueError("native_preparation_scope_mismatch")
            if scope.primary_key != tuple((context["table"]["key_columns"] or "").split(",")):
                raise ValueError("native_preparation_key_mismatch")
            declared = context["table"]["discovered_columns"]
            kinds = {"smallint": "int", "integer": "int", "bigint": "int", "decimal": "decimal",
                     "numeric": "decimal", "char": "text", "varchar": "text", "clob": "text",
                     "graphic": "text", "vargraphic": "text", "dbclob": "text",
                     "date": "date", "timestamp": "timestamp"}
            if (not declared or tuple((column.name, column.kind) for column in scope.columns)
                    != tuple((column["name"], kinds[column["kind"]]) for column in declared)):
                raise ValueError("native_preparation_columns_mismatch")
            refs = canonical(_refs(prepared.artifacts, _PREPARATION))
            scope_json = canonical(scope.to_dict())
        except Exception:
            self.close_incomplete(lease, reason_code="preparation_failed")
            return None
        with context_transaction(self.engine, self.org_id) as c:
            current = self._row(c, lease.request_id)
            self._lease(current, lease)
            self._context(c, current)
            c.execute(update(schema.native_verifications).where(
                schema.native_verifications.c.id == lease.request_id).values(
                    scope_json=scope_json, scope_sha256=scope.sha256, preparation_json=refs,
                    lifecycle="running", revision=current["revision"] + 1))
        return replace(lease, revision=lease.revision + 1)

    def close_incomplete(self, lease, *, reason_code):
        if reason_code not in _REASONS:
            raise ValueError("invalid_native_failure_reason")
        with context_transaction(self.engine, self.org_id) as c:
            row = self._row(c, lease.request_id)
            self._lease(row, lease)
            c.execute(update(schema.native_verifications).where(
                schema.native_verifications.c.id == lease.request_id).values(
                    lifecycle="finished", outcome="incomplete", reason_code=reason_code,
                    finished_at=_utc(self.now()), revision=row["revision"] + 1))
        return self.get(lease.request_id)

    def finalize(self, lease):
        with context_transaction(self.engine, self.org_id) as c:
            terminal = self._row(c, lease.request_id)
            if terminal["lifecycle"] == "finished":
                if (terminal["controller_holder"], terminal["lease_generation"], terminal["revision"]) != (
                        lease.holder, lease.lease_generation, lease.revision + 1):
                    raise NativeContextConflict("native_terminal_fence_conflict")
                return self._view(c, terminal)
        row = self._read_owned(lease)
        if not json.loads(row["context_json"])["organization"]["native_context_enforced"]:
            return self.close_incomplete(lease, reason_code="native_authority_unavailable")
        if row["scope_json"] is None:
            return self.close_incomplete(lease, reason_code="preparation_failed")
        if self.controller is None:
            return self.close_incomplete(lease, reason_code="admission_unavailable")
        try:
            scope = VerificationScope.from_dict(json.loads(row["scope_json"]))
            prepared = NativePreparation(scope, tuple(NativeArtifact(**x) for x in json.loads(row["preparation_json"])))
            admitted = self.controller.admit(prepared)
            if type(admitted) is not NativeAdmission:
                raise ValueError("independent_native_admission_required")
            report = admitted.comparison
            _text(admitted.policy_revision)
            refs = _refs(admitted.artifacts, _PROOFS | {"stream:" + n for n in DATASETS})
            _comparison(report, admitted.artifacts, scope, self.now())
            manifest = canonical(dict(
                format_version="quadringent-native-admitted-result-v1", scope_sha256=scope.sha256,
                context_sha256=row["context_sha256"], context_generation=row["context_generation"],
                policy_revision=admitted.policy_revision, comparison=report, artifacts=refs,
            ))
            digest = hashlib.sha256(manifest.encode()).hexdigest()
        except Exception:
            return self.close_incomplete(lease, reason_code="admission_failed")
        with context_transaction(self.engine, self.org_id) as c:
            current = self._row(c, lease.request_id)
            if current["lifecycle"] == "finished":
                if current["result_sha256"] != digest:
                    raise NativeContextConflict("native_terminal_result_conflict")
            else:
                self._lease(current, lease)
                self._context(c, current)
                c.execute(update(schema.native_verifications).where(
                    schema.native_verifications.c.id == lease.request_id).values(
                        lifecycle="finished", outcome=report["state"], finished_at=_utc(self.now()),
                        result_manifest_json=manifest, result_sha256=digest, revision=current["revision"] + 1))
        return self.get(lease.request_id)

    def _view(self, c, row):
        p, context, _, digest = self._context(c, row, require_current=False)
        enforced = context["organization"]["native_context_enforced"]
        same = (p["latest_native_verification_id"] == row["id"] and p["native_context_operation_id"] is None
                and p["native_context_generation"] == row["context_generation"] and digest == row["context_sha256"])
        outcome, reason, manifest, observed, age = row["outcome"], row["reason_code"], None, None, None
        if row["result_manifest_json"] is not None:
            try:
                encoded = row["result_manifest_json"]
                manifest = json.loads(encoded)
                if (hashlib.sha256(encoded.encode()).hexdigest() != row["result_sha256"]
                        or manifest.get("format_version") != "quadringent-native-admitted-result-v1"
                        or manifest.get("scope_sha256") != row["scope_sha256"]
                        or manifest.get("context_sha256") != row["context_sha256"]):
                    raise ValueError("stored_result_invalid")
                observed = max((v["completed_at"] for v in manifest["comparison"]["datasets"].values()),
                               key=_timestamp)
                age = (_utc(self.now()) - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds()
                if age < 0:
                    raise ValueError("stored_result_future")
            except (ValueError, TypeError, AttributeError, KeyError):
                outcome, reason, manifest = "incomplete", "stored_result_invalid", None
                observed, age = None, None
        return dict(
            id=row["id"], pipeline_id=row["pipeline_id"], table_id=row["table_id"], source_id=row["source_id"],
            destination_id=row["destination_id"], copy_run_id=row["copy_run_id"], lifecycle=row["lifecycle"],
            outcome=outcome, reason_code=reason,
            created_at=_sql_time(row["created_at"]).isoformat(),
            finished_at=_sql_time(row["finished_at"]).isoformat() if row["finished_at"] else None,
            context_generation=row["context_generation"], context_sha256=row["context_sha256"],
            scope_sha256=row["scope_sha256"],
            context_validity=("same_context" if same else "superseded") if enforced else "tracking_unavailable",
            context_enforcement="enabled" if enforced else "unavailable",
            head_freshness="unknown", freshness_policy_revision=None, expires_at=None,
            observed_at=observed, age_seconds=age,
            result_manifest=manifest, result_sha256=row["result_sha256"],
        )

    def get(self, request_id):
        with self.engine.connect() as c:
            return self._view(c, self._row(c, request_id))

    def get_latest(self, pipeline_id):
        with self.engine.connect() as c:
            pipeline, _, _, _ = context_snapshot(c, self.org_id, pipeline_id, self.source_restriction)
            request_id = pipeline["latest_native_verification_id"]
            return self._view(c, self._row(c, request_id)) if request_id else None

    def list_history(self, pipeline_id, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid_native_history_limit")
        with self.engine.connect() as c:
            context_snapshot(c, self.org_id, pipeline_id, self.source_restriction)
            rows = c.execute(select(schema.native_verifications).where(
                schema.native_verifications.c.org_id == self.org_id,
                schema.native_verifications.c.pipeline_id == pipeline_id,
            ).order_by(schema.native_verifications.c.created_at.desc(), schema.native_verifications.c.id.desc())
                .limit(limit)).mappings().all()
            return tuple(self._view(c, row) for row in rows)
