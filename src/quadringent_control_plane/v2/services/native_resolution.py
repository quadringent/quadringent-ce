"""Resolve exact preparation archives without replacing originals with live state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time
from types import MappingProxyType
from urllib.parse import quote

from quadringent.checkpoint import CheckpointObservation
from quadringent.object_store import _validate_object_key
from quadringent.qualification.verification_contract import VerificationScope
from quadringent.qualification.verification_native import load_native_copy_inputs
from quadringent.qualification.verification_stored import ObjectReference, StoredLimits, export_stored
from quadringent.qualification.verification_stream import StreamLimits
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.snowflake_destination import default_history_table_name, default_mirror_table_name
from quadringent.storage_backend import registry_authority
from ..executor.evidence import copy_intent_key, evidence_key
from .native_artifacts import artifact_key, storage_prefix
from .native_context import canonical
from .native_preparation import StoredNativePreparer, _json
from .native_verifications import NativeArtifact, NativePreparation


class NativeResolutionError(ValueError):
    """An archived preparation could not be resolved; stored values are omitted."""


def _require(condition):
    if not condition:
        raise NativeResolutionError("native_resolution_invalid_input")


@dataclass(frozen=True)
class ResolutionLimits:
    max_object_bytes: int = 32 * 1024**2
    max_total_bytes: int = 16 * 1024**3
    max_objects: int = 100_000
    max_seconds: int = 1800
    max_rows: int = 20_000_000

    def __post_init__(self):
        for value, cap in ((self.max_object_bytes, 64 * 1024**2), (self.max_total_bytes, 16 * 1024**3),
                           (self.max_objects, 100_000), (self.max_seconds, 7200), (self.max_rows, 20_000_000)):
            _require(type(value) is int and 1 <= value <= cap)


class ReadBudget:
    """Bound repeated reads and cooperative deadlines around provider calls."""

    def __init__(self, limits, check_current):
        _require(callable(check_current))
        self.limits, self.check_current = limits, check_current
        self.started = self.previous = time.monotonic()
        self.bytes = self.objects = 0
        self.check()

    def check(self):
        now = time.monotonic()
        _require(type(now) in (int, float) and math.isfinite(now) and now >= self.previous
                 and now - self.started < self.limits.max_seconds and self.check_current() is True)
        self.previous = now
        return True

    def read(self, store, key, maximum):
        self.check()
        _validate_object_key(key)
        _require(type(maximum) is int and maximum > 0 and self.objects < self.limits.max_objects)
        maximum = min(maximum, self.limits.max_object_bytes, self.limits.max_total_bytes - self.bytes)
        _require(maximum > 0)
        self.objects += 1
        content = store.get_bounded(key, maximum)
        self.check()
        _require(type(content) is bytes and 0 < len(content) <= maximum)
        self.bytes += len(content)
        return content


class PinnedArchiveStore:
    """Read-only source-key overlay; every read verifies archived original bytes."""

    def __init__(self, storage, uri, prefix, references, budget):
        self.storage, self.uri, self.prefix, self.budget = storage, uri, prefix, budget
        self.references = MappingProxyType(dict(references))
        self.used = set()
        self._store = None

    def read_reference(self, reference, maximum):
        self.budget.check()
        key = artifact_key(reference, self.uri, self.prefix)
        _require(type(maximum) is int and reference.byte_count <= maximum)
        if self._store is None:
            self._store = self.storage.object_store("")
        content = self.budget.read(self._store, key, reference.byte_count)
        _require(len(content) == reference.byte_count and hashlib.sha256(content).hexdigest() == reference.sha256)
        return content

    def get_bounded(self, key, maximum):
        try:
            _validate_object_key(key)
            _require(key in self.references)
            content = self.read_reference(self.references[key], maximum)
            self.used.add(key)
            return content
        except Exception:
            raise NativeResolutionError("native_resolution_original_unavailable") from None


class AuthorityStore:
    def __init__(self, store):
        self.store = store

    def get_bounded(self, key, maximum):
        _validate_object_key(key)
        return self.store.get_bounded("receiver-authority-v1/" + key, maximum)


class _Discard:
    def __init__(self, budget):
        self.budget = budget

    def write(self, value):
        self.budget.check()
        return len(value)

    def flush(self):
        self.budget.check()


@dataclass(frozen=True)
class ResolvedPreparation:
    scope: VerificationScope
    org_id: str
    raw_prefix_root: str
    backend: str
    raw_bucket: str
    source_id: str
    destination_id: str
    context_sha256: str
    initial_copy: ObjectReference
    copy_intent: ObjectReference
    store: PinnedArchiveStore
    artifacts: tuple[NativeArtifact, ...]

    def verify(self):
        try:
            for reference in self.artifacts:
                self.store.read_reference(reference, reference.byte_count)
            for key, reference in self.store.references.items():
                self.store.get_bounded(key, reference.byte_count)
            self.store.budget.check()
        except Exception:
            raise NativeResolutionError("native_resolution_original_unavailable") from None


class PreparationResolver:
    def __init__(self, storage, raw_prefix_root, org_id, *, limits=ResolutionLimits()):
        self.uri, self.prefix = storage_prefix(storage, org_id, "native-preparations-v1")
        _validate_object_key(raw_prefix_root)
        _require(type(limits) is ResolutionLimits)
        self.storage, self.root, self.org_id, self.limits = storage, raw_prefix_root, org_id, limits

    def resolve(self, prepared, *, context, check_current=lambda: True):
        try:
            return self._resolve(prepared, context, check_current)
        except Exception:
            raise NativeResolutionError("native_preparation_resolution_failed") from None

    def _resolve(self, prepared, context, check_current):
        budget = ReadBudget(self.limits, check_current)
        _require(type(prepared) is NativePreparation and type(prepared.scope) is VerificationScope
                 and type(prepared.artifacts) is tuple and len(prepared.artifacts) == 6)
        scope = prepared.scope
        roles = {a.name: a for a in prepared.artifacts if type(a) is NativeArtifact}
        _require(set(roles) == {"copy_intent", "copy_evidence", "registry", "binding", "checkpoint", "preparation_manifest"})
        for ref in roles.values():
            artifact_key(ref, self.uri, self.prefix)
        columns, keys = StoredNativePreparer(
            self.storage, self.root, self.org_id, scope.producer_revision, scope.producer_version
        )._context(context)
        table, destination = context["table"], context["destination"]
        _require((scope.pipeline_id, scope.table_id, scope.copy_run_id, scope.source_library, scope.source_table,
                  scope.destination_database, scope.destination_schema, scope.history_table, scope.mirror_table,
                  scope.columns, scope.primary_key)
                 == (context["pipeline_id"], context["table_id"], context["copy_run_id"], table["schema_name"],
                     table["table_name"], destination["destination_database"], destination["destination_schema"],
                     default_history_table_name(table["table_name"]), default_mirror_table_name(table["table_name"]),
                     columns, keys))
        archive = PinnedArchiveStore(self.storage, self.uri, self.prefix, {}, budget)
        document = _json(archive.read_reference(roles["preparation_manifest"], self.limits.max_object_bytes))
        _require(type(document) is dict and set(document) == {
            "format_version", "native_admission", "native_cdc_qualified", "context_sha256", "scope", "org_id",
            "source_id", "destination_id", "registry_inventory", "checkpoint_observation", "artifacts", "originals"})
        context_digest = hashlib.sha256(canonical(context).encode()).hexdigest()
        _require(document["format_version"] == "quadringent-native-preparation-v1"
                 and document["native_admission"] == "not_evaluated" and document["native_cdc_qualified"] is False
                 and document["context_sha256"] == context_digest and document["scope"] == scope.to_dict()
                 and document["org_id"] == self.org_id and document["source_id"] == context["source_id"]
                 and document["destination_id"] == context["destination_id"]
                 and document["artifacts"] == [r.to_dict() for r in prepared.artifacts if r.name != "preparation_manifest"])
        originals = document["originals"]
        _require(type(originals) is list and 1 <= len(originals) <= self.limits.max_objects)
        references = {}
        for index, item in enumerate(originals):
            _require(type(item) is dict and set(item) == {"source_key", "source_uri", "archived_uri", "sha256", "byte_count", "version"})
            key = item["source_key"]
            _validate_object_key(key)
            _require(key not in references and item["source_uri"] == self.uri + quote(key, safe="/"))
            ref = NativeArtifact("original:" + str(index), item["archived_uri"], item["sha256"], item["version"], item["byte_count"])
            artifact_key(ref, self.uri, self.prefix)
            references[key] = ref
        store = PinnedArchiveStore(self.storage, self.uri, self.prefix, references, budget)
        stream_key = reader_checkpoint_stream_key(
            storage_backend=self.storage.kind, source_id=context["source_id"], journal_library=table["journal_library"],
            journal_name=table["journal_name"], raw_bucket=self.storage.raw_bucket, raw_prefix=self.root)
        _require(scope.capture_binding.scope_id == stream_key
                 and scope.capture_binding.registry_authority == registry_authority(self.storage.kind, self.storage.raw_bucket)
                 and (scope.copy_boundary.receiver_identity.journal_library, scope.copy_boundary.receiver_identity.journal_name)
                 == (table["journal_library"], table["journal_name"]))
        c = document["checkpoint_observation"]
        _require(type(c) is dict and set(c) == {"backend", "state_location", "stream_key", "encoding", "version", "artifact"})
        _require((c["backend"], c["state_location"], c["stream_key"], c["encoding"], c["artifact"])
                 == (self.storage.kind, self.storage.state_location, stream_key,
                     "dynamodb-attribute-item-v1" if self.storage.kind == "aws" else "checkpoint-json-v2",
                     {k: v for k, v in roles["checkpoint"].to_dict().items() if k != "name"}))
        checkpoint = CheckpointObservation(scope.checkpoint, store.read_reference(roles["checkpoint"], 65536), c["encoding"], c["version"])
        _require(checkpoint.require_stream(stream_key) == scope.checkpoint)
        registry = ReceiverNameRegistry(AuthorityStore(store), authority=scope.capture_binding.registry_authority,
                                        source_system="ibmi", journal=scope.capture_binding.journal)
        _require(registry.load_binding(stream_key) == scope.capture_binding)
        for position in (scope.copy_boundary, scope.checkpoint):
            registry.require(scope.capture_binding, position)
        registry_keys = sorted({registry.binding_key(stream_key), *(registry.claim_key(p.receiver_identity.receiver_name)
                                 for p in (scope.capture_binding.bootstrap, scope.copy_boundary, scope.checkpoint))})
        inventory = document["registry_inventory"]
        _require(inventory == {"coverage": "exact_keys_used", "authority": scope.capture_binding.registry_authority,
                              "keys": registry_keys, "artifact": {k: v for k, v in roles["registry"].to_dict().items() if k != "name"}})
        registry.verify_inventory(store.read_reference(roles["registry"], self.limits.max_object_bytes),
                                  expected_sha256=roles["registry"].sha256, max_keys=self.limits.max_objects,
                                  max_bytes=min(self.limits.max_object_bytes, 16 * 1024**2))
        initial_key = evidence_key(self.root, scope.table_id, scope.copy_run_id)
        intent_key = "receiver-authority-v1/" + copy_intent_key(scope.pipeline_id, scope.table_id, scope.copy_run_id)
        for key, role in ((initial_key, "copy_evidence"), (intent_key, "copy_intent"),
                          ("receiver-authority-v1/" + registry.binding_key(stream_key), "binding")):
            _require(key in references and references[key].uri == roles[role].uri
                     and references[key].sha256 == roles[role].sha256
                     and references[key].byte_count == roles[role].byte_count)
        initial_copy, intent = ObjectReference(initial_key, roles["copy_evidence"].sha256), ObjectReference(intent_key, roles["copy_intent"].sha256)
        load_native_copy_inputs(AuthorityStore(store), store.get_bounded(initial_key, 65536),
                                expected_sha256=initial_copy.sha256, evidence_key=initial_key, scope=scope)
        _require(scope.expected_snapshot_rows <= self.limits.max_rows)
        export_stored(store, scope, _Discard(budget), dataset="snapshot", raw_prefix_root=self.root,
                      initial_copy=initial_copy, copy_intent=intent,
                      limits=StreamLimits(max_rows=self.limits.max_rows, max_bytes=self.limits.max_total_bytes,
                                          max_seconds=self.limits.max_seconds),
                      stored_limits=StoredLimits(max_object_bytes=self.limits.max_object_bytes,
                                                 max_total_bytes=self.limits.max_total_bytes,
                                                 max_objects=self.limits.max_objects))
        _require(store.used == set(references))
        result = ResolvedPreparation(scope, self.org_id, self.root, self.storage.kind, self.storage.raw_bucket,
                                     context["source_id"], context["destination_id"], context_digest,
                                     initial_copy, intent, store, prepared.artifacts)
        result.verify()
        return result
