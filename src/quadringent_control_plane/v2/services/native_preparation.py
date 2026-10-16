"""Archive bounded stored inputs; preparation grants no native admission or stable cut.

Deadlines are checked around I/O; storage calls require their own network limits.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from urllib.parse import quote

from quadringent.checkpoint import CheckpointObservation
from quadringent.copy_observation import read_copy_observations, validate_inventory_originals
from quadringent.object_store import _validate_object_key
from quadringent.qualification.verification_contract import Column, ReceiverWindow, VerificationScope
from quadringent.qualification.verification_stored import ObjectReference, StoredLimits, export_stored
from quadringent.qualification.verification_stream import StreamLimits
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.snowflake_destination import default_history_table_name, default_mirror_table_name
from quadringent.storage_backend import StorageBackend, registry_authority
from quadringent.storage_layout import snapshot_prefix
from ..executor.evidence import EvidenceReader, copy_intent_key, evidence_key
from .native_context import canonical
from .native_verifications import NativeArtifact, NativePreparation


_FORMAT = "quadringent-native-preparation-v1"
_KINDS = {
    "smallint": "int",
    "integer": "int",
    "bigint": "int",
    "decimal": "decimal",
    "numeric": "decimal",
    "char": "text",
    "varchar": "text",
    "clob": "text",
    "graphic": "text",
    "vargraphic": "text",
    "dbclob": "text",
    "date": "date",
    "timestamp": "timestamp",
}


class NativePreparationError(ValueError):
    """Preparation failed without exposing stored values or provider messages."""


def _require(condition):
    if not condition:
        raise NativePreparationError("native_preparation_invalid_input")


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result

    def constant(_):
        raise NativePreparationError("native_preparation_invalid_json")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


def _text(value):
    _require(
        type(value) is str
        and 1 <= len(value) <= 256
        and value.strip() == value
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


@dataclass(frozen=True)
class PreparationLimits:
    """Aggregate bounds include repeated reads, writes and durable readbacks."""

    max_object_bytes: int = 32 * 1024**2
    max_total_bytes: int = 8 * 1024**3
    max_objects: int = 10_000
    max_rows: int = 2_000_000
    max_seconds: int = 120

    def __post_init__(self):
        for value, cap in (
            (self.max_object_bytes, 64 * 1024**2),
            (self.max_total_bytes, 16 * 1024**3),
            (self.max_objects, 100_000),
            (self.max_rows, 20_000_000),
            (self.max_seconds, 1800),
        ):
            _require(type(value) is int and 1 <= value <= cap)


class _Budget:
    def __init__(self, limits):
        self.limits = limits
        self.started = self.previous = time.monotonic()
        self.objects = self.bytes = 0
        self.check()

    def check(self):
        now = time.monotonic()
        _require(
            type(now) in (int, float)
            and math.isfinite(now)
            and now >= self.previous
            and now - self.started < self.limits.max_seconds
        )
        self.previous = now

    def allowance(self, maximum):
        self.check()
        _require(self.objects < self.limits.max_objects and self.bytes < self.limits.max_total_bytes)
        return min(maximum, self.limits.max_object_bytes, self.limits.max_total_bytes - self.bytes)

    def charge(self, content, maximum):
        self.check()
        _require(
            type(content) is bytes
            and 0 < len(content) <= maximum
            and self.bytes + len(content) <= self.limits.max_total_bytes
        )
        self.bytes += len(content)

    def begin(self, maximum):
        maximum = self.allowance(maximum)
        self.objects += 1
        return maximum

    def read(self, store, key, maximum):
        _validate_object_key(key)
        maximum = self.begin(maximum)
        content = store.get_bounded(key, maximum)
        self.charge(content, maximum)
        return content


class _Archive:
    def __init__(self, store, budget, bucket, scheme, org_id):
        self.store, self.budget = store, budget
        self.uri = scheme + "://" + bucket + "/"
        self.prefix = "native-preparations-v1/" + _digest(org_id.encode()) + "/objects/"
        self.refs = {}

    def retain(self, content):
        digest = _digest(content)
        key = self.prefix + digest
        if digest not in self.refs:
            maximum = self.budget.begin(len(content))
            _require(len(content) <= maximum)
            self.store.put_once(key, content)
            self.budget.charge(content, maximum)
            _require(self.budget.read(self.store, key, len(content)) == content)
            self.refs[digest] = {
                "uri": self.uri + key,
                "sha256": digest,
                "byte_count": len(content),
                "version": "sha256:" + digest,
            }
        _require(self.refs[digest]["byte_count"] == len(content))
        return deepcopy(self.refs[digest])

    def verify(self):
        for key, value in self.refs.items():
            content = self.budget.read(self.store, self.prefix + key, value["byte_count"])
            _require(_digest(content) == key and len(content) == value["byte_count"])


class _Originals:
    def __init__(self, store, budget, archive):
        self.store, self.budget, self.archive = store, budget, archive
        self.refs = {}

    def get_bounded(self, key, maximum):
        content = self.budget.read(self.store, key, maximum)
        digest = _digest(content)
        if key in self.refs:
            _require((digest, len(content)) == (self.refs[key]["sha256"], self.refs[key]["byte_count"]))
        else:
            durable = self.archive.retain(content)
            self.refs[key] = {
                "source_key": key,
                "source_uri": self.archive.uri + quote(key, safe="/"),
                "archived_uri": durable["uri"],
                "sha256": digest,
                "byte_count": len(content),
                "version": durable["version"],
            }
        return content

    def artifact(self, name, key):
        value = self.refs[key]
        return NativeArtifact(name, value["archived_uri"], value["sha256"], value["version"], value["byte_count"])

    def verify(self):
        for key, value in tuple(self.refs.items()):
            self.get_bounded(key, value["byte_count"])


class _Prefix:
    def __init__(self, originals, prefix):
        self.originals, self.prefix = originals, prefix

    def get_bounded(self, key, maximum):
        _validate_object_key(key)
        return self.originals.get_bounded(self.prefix + "/" + key, maximum)


class _Discard:
    def __init__(self, budget):
        self.budget = budget

    def write(self, value):
        self.budget.check()
        return len(value)

    def flush(self):
        self.budget.check()


class StoredNativePreparer:
    def __init__(
        self,
        storage: StorageBackend,
        raw_prefix_root: str,
        org_id: str,
        producer_revision: str,
        producer_version: str,
        *,
        limits=PreparationLimits(),
    ):
        _require(
            type(storage) is StorageBackend
            and storage.kind in {"aws", "gcs", "local"}
            and type(limits) is PreparationLimits
        )
        _require(
            type(storage.raw_bucket) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,222}", storage.raw_bucket) is not None
        )
        _validate_object_key(raw_prefix_root)
        _text(org_id)
        _text(producer_version)
        _require(type(producer_revision) is str and re.fullmatch(r"[0-9a-f]{40}", producer_revision) is not None)
        self.storage, self.root, self.org_id = storage, raw_prefix_root, org_id
        self.revision, self.version, self.limits = producer_revision, producer_version, limits

    def _context(self, context):
        _require(
            type(context) is dict
            and set(context)
            == {
                "org_id",
                "pipeline_id",
                "table_id",
                "source_id",
                "destination_id",
                "copy_run_id",
                "generation",
                "source",
                "table",
                "destination",
                "organization",
            }
        )
        _require(context["org_id"] == self.org_id and type(context["generation"]) is int and context["generation"] >= 0)
        for key in ("org_id", "pipeline_id", "table_id", "source_id", "destination_id", "copy_run_id"):
            _text(context[key])
        expected = {
            "source": {"ibmi_host", "ibmi_user", "tls_trust", "detected_timezone", "paused_at", "tls_pin_sha256"},
            "table": {
                "key_strategy",
                "key_columns",
                "journal_library",
                "journal_name",
                "journal_status",
                "images",
                "discovered_columns",
                "schema_name",
                "table_name",
            },
            "destination": {
                "snowflake_account",
                "destination_database",
                "destination_schema",
                "service_user",
                "service_role",
                "paused_at",
            },
            "organization": {"paused_at", "native_context_enforced"},
        }
        for name, fields in expected.items():
            _require(type(context[name]) is dict and set(context[name]) == fields)
        _require(
            context["organization"]["native_context_enforced"] is True
            and all(context[name]["paused_at"] is False for name in ("organization", "source", "destination"))
        )
        table = context["table"]
        _require(
            table["key_strategy"] in {"primary", "unique_index"}
            and table["images"] == "*BOTH"
            and type(table["key_columns"]) is str
            and type(table["discovered_columns"]) is list
            and 1 <= len(table["discovered_columns"]) <= 2048
        )
        columns = tuple(Column(value["name"], _KINDS[value["kind"]]) for value in table["discovered_columns"])
        canonical(context)
        return columns, tuple(table["key_columns"].split(","))

    def prepare(self, context):
        try:
            return self._prepare(deepcopy(context))
        except Exception:
            raise NativePreparationError("native_preparation_failed") from None

    def _prepare(self, context):
        columns, keys = self._context(context)
        budget = _Budget(self.limits)
        table, destination = context["table"], context["destination"]
        scope_id = reader_checkpoint_stream_key(
            storage_backend=self.storage.kind,
            source_id=context["source_id"],
            journal_library=table["journal_library"],
            journal_name=table["journal_name"],
            raw_bucket=self.storage.raw_bucket,
            raw_prefix=self.root,
        )
        store = self.storage.object_store("")
        scheme = {"aws": "s3", "gcs": "gcs", "local": "local"}[self.storage.kind]
        archive = _Archive(store, budget, self.storage.raw_bucket, scheme, self.org_id)
        originals = _Originals(store, budget, archive)
        authority = _Prefix(originals, "receiver-authority-v1")
        key = evidence_key(self.root, context["table_id"], context["copy_run_id"])
        evidence = EvidenceReader(originals).read(key, expected_source=(table["schema_name"], table["table_name"]))
        _require(evidence is not None and evidence.copy_observations is not None)
        _require(
            (evidence.pipeline_id, evidence.table_id, evidence.run_id)
            == (context["pipeline_id"], context["table_id"], context["copy_run_id"])
        )
        boundary, binding = evidence.require_bound(), evidence.boundary.capture_binding
        _require(
            binding.scope_id == scope_id
            and binding.registry_authority == registry_authority(self.storage.kind, self.storage.raw_bucket)
            and (boundary.receiver_identity.journal_library, boundary.receiver_identity.journal_name)
            == (table["journal_library"], table["journal_name"])
        )
        intent_key = "receiver-authority-v1/" + copy_intent_key(
            context["pipeline_id"], context["table_id"], context["copy_run_id"]
        )
        intent = _json(originals.get_bounded(intent_key, 65536))
        _require(
            intent
            == {
                "format_version": "quadringent-copy-intent-v1",
                "pipeline_id": context["pipeline_id"],
                "table_id": context["table_id"],
                "run_id": context["copy_run_id"],
                "boundary": evidence.boundary.to_dict(),
            }
        )
        checkpoint_store = self.storage.checkpoint_store(scope_id)
        maximum = budget.begin(65536)
        _require(maximum == 65536)
        observation = checkpoint_store.observe_bound()
        budget.check()
        _require(type(observation) is CheckpointObservation)
        _require(
            observation.encoding
            == ("dynamodb-attribute-item-v1" if self.storage.kind == "aws" else "checkpoint-json-v2")
        )
        checkpoint = observation.require_stream(scope_id)
        budget.charge(observation.content, maximum)
        binding.validate_checkpoint(checkpoint)
        _require(
            checkpoint.receiver_identity == boundary.receiver_identity and checkpoint.sequence >= boundary.sequence
        )
        registry = ReceiverNameRegistry(
            authority, authority=binding.registry_authority, source_system="ibmi", journal=binding.journal
        )
        registry.require(binding, boundary)
        registry.require(binding, checkpoint)
        manifest = read_copy_observations(
            authority,
            evidence.copy_observations,
            expected_batches=[value.to_dict() for value in evidence.snapshot_batches],
        )
        _require(
            all(
                item["prefix"] == snapshot_prefix(self.root, table["table_name"])
                for item in manifest["snapshot_inventory"]
            )
        )
        validate_inventory_originals(manifest, lambda prefix: _Prefix(originals, prefix))
        for phase in ("before", "after"):
            raw = originals.get_bounded(
                "receiver-authority-v1/" + manifest[phase]["key"], manifest[phase]["byte_count"]
            )
            description = _json(raw)["format"]["fields"]
            _require(tuple((f["sql_name"], f["kind"]) for f in description) == tuple((c.name, c.kind) for c in columns))
            for declared, field in zip(table["discovered_columns"], description, strict=True):
                for declared_name, native_name in (("precision", "digits"), ("scale", "scale")):
                    if declared.get(declared_name) is not None:
                        _require(type(declared[declared_name]) is int and declared[declared_name] == field[native_name])
        scope = VerificationScope(
            context["pipeline_id"],
            context["table_id"],
            context["copy_run_id"],
            table["schema_name"],
            table["table_name"],
            destination["destination_database"],
            destination["destination_schema"],
            default_history_table_name(table["table_name"]),
            default_mirror_table_name(table["table_name"]),
            columns,
            keys,
            binding,
            boundary,
            checkpoint,
            (ReceiverWindow(boundary.receiver_identity, boundary.sequence + 1, checkpoint.sequence),),
            evidence.rows_copied,
            self.revision,
            self.version,
        )
        _require(scope.expected_snapshot_rows <= self.limits.max_rows)
        export_stored(
            originals,
            scope,
            _Discard(budget),
            dataset="snapshot",
            raw_prefix_root=self.root,
            initial_copy=ObjectReference(key, originals.refs[key]["sha256"]),
            copy_intent=ObjectReference(intent_key, originals.refs[intent_key]["sha256"]),
            limits=StreamLimits(
                max_rows=self.limits.max_rows,
                max_bytes=self.limits.max_total_bytes,
                max_seconds=self.limits.max_seconds,
            ),
            stored_limits=StoredLimits(
                max_object_bytes=self.limits.max_object_bytes,
                max_total_bytes=self.limits.max_total_bytes,
                max_objects=self.limits.max_objects,
            ),
        )
        registry_keys = sorted(
            {
                registry.binding_key(scope_id),
                *(
                    registry.claim_key(point.receiver_identity.receiver_name)
                    for point in (binding.bootstrap, boundary, checkpoint)
                ),
            }
        )
        inventory = registry.export_inventory(
            registry_keys, max_keys=self.limits.max_objects, max_bytes=min(self.limits.max_object_bytes, 16 * 1024**2)
        )
        registry.verify_inventory(
            inventory,
            expected_sha256=_digest(inventory),
            max_keys=self.limits.max_objects,
            max_bytes=min(self.limits.max_object_bytes, 16 * 1024**2),
        )
        checkpoint_ref, inventory_ref = archive.retain(observation.content), archive.retain(inventory)
        artifacts = (
            originals.artifact("copy_intent", intent_key),
            originals.artifact("copy_evidence", key),
            NativeArtifact("registry", **inventory_ref),
            originals.artifact("binding", "receiver-authority-v1/" + registry.binding_key(scope_id)),
            NativeArtifact(
                "checkpoint",
                checkpoint_ref["uri"],
                checkpoint_ref["sha256"],
                checkpoint_ref["version"],
                checkpoint_ref["byte_count"],
            ),
        )
        originals.verify()
        archive.verify()
        document = {
            "format_version": _FORMAT,
            "native_admission": "not_evaluated",
            "native_cdc_qualified": False,
            "context_sha256": _digest(canonical(context).encode()),
            "scope": scope.to_dict(),
            "org_id": self.org_id,
            "source_id": context["source_id"],
            "destination_id": context["destination_id"],
            "registry_inventory": {
                "coverage": "exact_keys_used",
                "authority": binding.registry_authority,
                "keys": registry_keys,
                "artifact": inventory_ref,
            },
            "checkpoint_observation": {
                "backend": self.storage.kind,
                "state_location": self.storage.state_location,
                "stream_key": scope_id,
                "encoding": observation.encoding,
                "version": observation.version,
                "artifact": checkpoint_ref,
            },
            "artifacts": [item.to_dict() for item in artifacts],
            "originals": [value for _, value in sorted(originals.refs.items())],
        }
        final = archive.retain((canonical(document, maximum=self.limits.max_object_bytes) + "\n").encode())
        archive.verify()
        budget.check()
        return NativePreparation(scope, artifacts + (NativeArtifact("preparation_manifest", **final),))
