"""Recompute archived collection contents; replay alone cannot admit native CDC."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import os
from datetime import datetime
from pathlib import PurePosixPath
import shutil
import tempfile

from quadringent.qualification.verification_contract import DATASETS
from quadringent.qualification.verification_guard import _document, _guard_document
from quadringent.qualification.verification_ledger import DiskReconciler
from quadringent.qualification.verification_source_observation import validate_source_observations
from quadringent.qualification.verification_native import load_native_copy_inputs
from quadringent.qualification.verification_identity import (
    read_destination_identity, validate_destination_identity_pair,
)
from quadringent.qualification.verification_receivers import read_receiver_observation, validate_receiver_pair
from quadringent.qualification.verification_stored import (
    StoredLimits, export_stored,
)
from quadringent.qualification.verification_stream import FrameReader, StreamLimits
from quadringent.storage_layout import journal_prefix
from .native_artifacts import artifact_key, storage_prefix
from .native_collection import _Budget, _new_directory
from .native_inventory import inspect_raw_originals
from .native_resolution import AuthorityStore, PreparationResolver
from .native_verifications import NativeArtifact


class NativeReplayError(ValueError):
    """Archived originals could not be independently recomputed."""


def _require(condition):
    if not condition:
        raise ValueError("invalid_native_archive")


@dataclass(frozen=True)
class NativeArchiveReplay:
    _comparison: dict = field(repr=False)
    streams_replayed: int
    missing_authorities: tuple[str, ...]
    validated_observations: tuple[str, ...] = ()
    state: str = "incomplete"
    native_admission: str = "not_evaluated"
    native_cdc_qualified: bool = False
    collection_manifest: NativeArtifact | None = None
    scope_sha256: str | None = None
    context_sha256: str | None = None

    @property
    def comparison(self):
        return deepcopy(self._comparison)


class _Reader:
    def __init__(self, storage, org_id, budget, maximum):
        self.uri, self.prefix = storage_prefix(storage, org_id, "native-collections-v1")
        self.store, self.budget, self.maximum = storage.object_store(""), budget, maximum
        self.references = {}
        self.bytes = 0

    def reference(self, value, name=None):
        reference = NativeArtifact(**value) if type(value) is dict else value
        key = artifact_key(reference, self.uri, self.prefix)
        _require(name is None or reference.name == name)
        _require(reference.byte_count <= self.maximum)
        if key not in self.references:
            self.bytes += reference.byte_count
            _require(len(self.references) < 100_000 and self.bytes <= 16 * 1024**3)
            self.references[key] = reference.byte_count
        _require(self.references[key] == reference.byte_count)
        return reference, key

    def read(self, value, maximum=32 * 1024**2, name=None):
        reference, key = self.reference(value, name)
        _require(reference.byte_count <= maximum)
        data = self.budget.call(self.store.get_bounded, key, reference.byte_count)
        _require(type(data) is bytes and len(data) == reference.byte_count
                 and hashlib.sha256(data).hexdigest() == reference.sha256)
        return data

    def file(self, value, stack, name):
        reference, key = self.reference(value, name)
        output = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        self.budget.call(self.store.verify_file, key, expected_sha256=reference.sha256,
                         byte_count=reference.byte_count, check_current=self.budget.admit, output=output)
        output.seek(0)
        return output

    def verify(self, value):
        reference, key = self.reference(value)
        self.budget.call(self.store.verify_file, key, expected_sha256=reference.sha256,
                         byte_count=reference.byte_count, check_current=self.budget.admit)


class StoredCollectionReplay:
    def __init__(self, storage, raw_prefix_root, org_id, *, max_seconds=3600, max_file_bytes=2 * 1024**3,
                 destination_identity=None, receiver_config=None, java=None, classpath=None, environment=None):
        _require(type(max_seconds) is int and 1 <= max_seconds <= 7200
                 and type(max_file_bytes) is int and 1 <= max_file_bytes <= 16 * 1024**3)
        storage_prefix(storage, org_id, "native-collections-v1")
        self.storage, self.root, self.org_id = storage, raw_prefix_root, org_id
        self.seconds, self.maximum = max_seconds, max_file_bytes
        self.destination_identity, self.receiver_config = destination_identity, receiver_config
        self.java, self.classpath, self.environment = java, classpath, dict(environment or {})

    def replay(self, prepared, manifest, *, context, directory, check_current=lambda: True):
        root = None
        parent_descriptor = root_identity = None
        try:
            budget = _Budget(self.seconds, check_current)
            budget.admit()
            reader = _Reader(self.storage, self.org_id, budget, self.maximum)
            doc = _document(reader.read(manifest, name="collection_manifest"))
            _require(type(doc) is dict)
            observed = doc.get("format_version") == "quadringent-native-server-collection-v2"
            _require(set(doc) == {
                "format_version", "scope_sha256", "context_sha256", "source_id", "destination_id",
                "native_admission", "native_cdc_qualified", "transaction_consistency_verified",
                "physical_inventory_admission", "preparation", "raw_inventory", "streams", "originals",
                "comparison", "collection", "native_bundle", "guards", "guard_metadata", "files"}
                | ({"authority_observations"} if observed else set()))
            _require(doc["format_version"] in {"quadringent-native-server-collection-v1", "quadringent-native-server-collection-v2"}
                     and doc["native_admission"] == "not_evaluated" and doc["native_cdc_qualified"] is False
                     and doc["physical_inventory_admission"] == "not_evaluated"
                     and doc["transaction_consistency_verified"] is False)
            resolved = budget.call(PreparationResolver(self.storage, self.root, self.org_id).resolve,
                                   prepared, context=context, check_current=budget.admit)
            scope = resolved.scope
            _require((doc["scope_sha256"], doc["context_sha256"], doc["source_id"], doc["destination_id"])
                     == (scope.sha256, resolved.context_sha256, resolved.source_id, resolved.destination_id)
                     and doc["preparation"] == [r.to_dict() for r in resolved.artifacts])
            copy = load_native_copy_inputs(AuthorityStore(resolved.store),
                resolved.store.get_bounded(resolved.initial_copy.key, 65536),
                expected_sha256=resolved.initial_copy.sha256, evidence_key=resolved.initial_copy.key, scope=scope)
            _require(reader.read(doc["native_bundle"], name="native_bundle") == copy.bundle)
            journal_guard, final_guard = self._guards(reader, doc, copy, scope)
            files = self._files(reader, doc)
            inventory, store, read_set = self._inventory(reader, doc, resolved)
            report = _document(reader.read(doc["comparison"], name="comparison"))
            _require(type(doc["streams"]) is dict and set(doc["streams"]) == set(DATASETS))
            root = _new_directory(directory)
            info = root.stat()
            root_identity = info.st_dev, info.st_ino
            parent_descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            current = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
            _require((current.st_dev, current.st_ino) == root_identity)
            source_observation = budget.call(validate_source_observations, copy, scope, journal_guard, final_guard,
                java=self.java, classpath=self.classpath,
                environment={k: v for k, v in self.environment.items()
                             if k in {"PATH", "JAVA_HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"}},
                directory=root / "native-source-observations", check_current=budget.admit,
                max_seconds=min(60, budget.remaining()))
            _require(source_observation["native_admission"] == "not_evaluated"
                     and source_observation["validated_current_observations"] == 3)
            limits = StreamLimits(max_bytes=self.maximum, max_seconds=min(1800, budget.remaining()))
            with ExitStack() as stack:
                streams = {}
                with DiskReconciler(root / "ledger", scope, max_seconds=budget.remaining()) as ledger:
                    for name in DATASETS:
                        item = doc["streams"][name]
                        _require(type(item) is dict and set(item) == {"artifact", "summary"})
                        stream = reader.file(item["artifact"], stack, "stream:" + name)
                        frame = FrameReader(stream, scope_sha256=scope.sha256, dataset=name, limits=limits)
                        budget.call(ledger.ingest, name, frame)
                        _require(frame.summary is not None and frame.summary.to_dict() == item["summary"])
                        streams[name] = frame.summary
                    actual = budget.call(ledger.finish)
                _require(actual["state"] in {"verified", "divergent"} and actual == report)
                for name in ("snapshot", "raw"):
                    output = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
                    summary = budget.call(export_stored, store, scope, output, dataset=name,
                        raw_prefix_root=self.root, initial_copy=resolved.initial_copy, copy_intent=resolved.copy_intent,
                        raw_read_set=read_set if name == "raw" else None, limits=limits, stored_limits=StoredLimits())
                    _require((summary.row_count, summary.rows_sha256)
                             == (streams[name].row_count, streams[name].rows_sha256))
                for value in files.values():
                    reader.verify(value)
                self._collection(reader, doc, files, scope, streams, limits, store.trace)
                observations = self._observations(reader, doc, files, scope)
                observations.append("native_source_observations")
                budget.call(resolved.verify)
            _require(inventory["coverage"] == "observed_keys_only")
            budget.admit()
            missing = ["platform", "source_provenance", "receiver_chain", "physical_inventory", "stable_cut"]
            if "destination_identity" not in observations:
                missing.append("destination_identity")
            return NativeArchiveReplay(deepcopy(actual), 7, tuple(missing), tuple(observations),
                collection_manifest=reader.reference(manifest, "collection_manifest")[0],
                scope_sha256=scope.sha256, context_sha256=resolved.context_sha256)
        except Exception:
            raise NativeReplayError("native_archive_replay_failed") from None
        finally:
            if parent_descriptor is not None:
                try:
                    current = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == root_identity:
                        shutil.rmtree(root.name, dir_fd=parent_descriptor)
                        os.fsync(parent_descriptor)
                except OSError:
                    pass
                finally:
                    os.close(parent_descriptor)

    @staticmethod
    def _guards(reader, doc, copy, scope):
        pins = dict(scope_sha256=scope.sha256, context_sha256=copy.context_sha256,
                    capture_binding_sha256=copy.binding_sha256, copy_boundary_sha256=copy.boundary_sha256,
                    copy_run_id=scope.copy_run_id, bundle_sha256=copy.bundle_sha256)
        guards = doc["guards"]
        _require(type(guards) is dict and set(guards) == {"journal_original", "final_original", "stdout_original"})
        journal = reader.read(guards["journal_original"], name="guard:journal_original")
        final = reader.read(guards["final_original"], name="guard:final_original")
        stdout = reader.read(guards["stdout_original"], name="guard:stdout_original")
        _require(final == stdout)
        _guard_document(pins, _document(journal), "journal")
        _guard_document(pins, _document(final), "after_all_streams")
        _require(all(doc["guard_metadata"].get(k) == v for k, v in pins.items()))
        return journal, final

    @staticmethod
    def _files(reader, doc):
        _require(type(doc["files"]) is list and 1 <= len(doc["files"]) <= 1000)
        files = {}
        for item in doc["files"]:
            _require(type(item) is dict and set(item) == {"relative_path", "artifact"})
            path = PurePosixPath(item["relative_path"])
            _require(not path.is_absolute() and str(path) == item["relative_path"]
                     and ".." not in path.parts and path.parts and str(path) not in files)
            reader.reference(item["artifact"])
            files[str(path)] = item["artifact"]
        values = list(files.values())
        _require(len({v["name"] for v in values}) == len(values))
        required = [doc["comparison"], doc["collection"], doc["native_bundle"], *doc["guards"].values(),
                    *(v["artifact"] for v in doc["streams"].values()), *(v["artifact"] for v in doc["originals"])]
        observations = doc.get("authority_observations", {})
        if "receiver" in observations:
            required.extend(observations["receiver"].values())
        if "destination" in observations:
            for item in observations["destination"].values():
                required.extend([item["before"], item["after"]])
        for value in required:
            _require(value in values)
        _require({NativeArtifact(**v) for v in required} == {NativeArtifact(**v) for v in values})
        return files

    def _inventory(self, reader, doc, resolved):
        inventory = _document(reader.read(doc["raw_inventory"], name="raw_inventory"))
        _require(type(inventory) is dict and set(inventory) == {
            "format_version", "coverage", "native_admission", "native_cdc_qualified", "org_id", "scope_sha256",
            "context_sha256", "prefix", "receipts", "originals", "batches"})
        _require(inventory["format_version"] == "quadringent-native-raw-inventory-v1"
                 and inventory["coverage"] == "observed_keys_only" and inventory["native_admission"] == "not_evaluated"
                 and inventory["native_cdc_qualified"] is False and inventory["org_id"] == self.org_id
                 and inventory["scope_sha256"] == resolved.scope.sha256
                 and inventory["context_sha256"] == resolved.context_sha256
                 and inventory["prefix"] == reader.uri + journal_prefix(self.root, resolved.scope.source_table) + "/")
        references = {}
        _require(type(inventory["originals"]) is list and len(inventory["originals"]) <= 100_000)
        prefix = journal_prefix(self.root, resolved.scope.source_table) + "/"
        for index, item in enumerate(inventory["originals"]):
            _require(type(item) is dict and set(item) == {"source_key", "artifact"}
                     and type(item["source_key"]) is str and item["source_key"].startswith(prefix)
                     and item["source_key"] not in references
                     and item["source_key"] not in resolved.store.references)
            references[item["source_key"]] = item["artifact"]
            reader.read(item["artifact"], name="raw-original:" + str(index))
        _require(list(references) == sorted(references))
        class Store:
            def __init__(self):
                self.trace = {}

            def get_bounded(self, key, maximum):
                if key in references:
                    content = reader.read(references[key], maximum)
                else:
                    content = resolved.store.get_bounded(key, maximum)
                observed = (hashlib.sha256(content).hexdigest(), len(content))
                _require(key not in self.trace or self.trace[key] == observed)
                self.trace[key] = observed
                return content
        store = Store()
        read_set, receipts = inspect_raw_originals(resolved, self.root,
            tuple(key.removeprefix(prefix) for key in references), store, check_current=reader.budget.admit)
        batches = [{"manifest": {"key": b.manifest.key, "sha256": b.manifest.sha256},
                    "payload": {"key": b.payload.key, "sha256": b.payload.sha256},
                    "receipt": {"key": b.receipt.key, "sha256": b.receipt.sha256} if b.receipt else None}
                   for b in read_set.batches]
        _require(inventory["receipts"] == receipts and inventory["batches"] == batches)
        store.trace.clear()
        return inventory, store, read_set

    @staticmethod
    def _collection(reader, doc, files, scope, streams, limits, trace):
        inner = _document(reader.read(doc["collection"], name="collection"))
        _require(type(inner) is dict and set(inner) == {"format_version", "scope_sha256", "native_admission",
            "native_cdc_qualified", "context_enforcement", "comparison", "streams", "originals"}
            and inner["format_version"] == "quadringent-native-table-collection-v1"
            and inner["scope_sha256"] == doc["scope_sha256"] and inner["native_admission"] == "not_evaluated"
            and inner["native_cdc_qualified"] is False and inner["context_enforcement"] == "caller_configuration_fence_only"
            and set(inner["streams"]) == set(DATASETS))
        def original(record):
            value = files["seven-streams/" + record["path"]]
            _require((record["sha256"], record["byte_count"]) == (value["sha256"], value["byte_count"]))
            return value
        _require(original(inner["comparison"]) == doc["comparison"])
        for name in DATASETS:
            item = inner["streams"][name]
            _require(original(item) == doc["streams"][name]["artifact"]
                     and item["summary"] == doc["streams"][name]["summary"])
        _require(len(inner["originals"]) == len(doc["originals"]))
        windows = {name: [] for name in ("journal", "positions")}
        recorded = set()
        for item, outer in zip(inner["originals"], doc["originals"], strict=True):
            _require(set(item) == {"path", "sha256", "byte_count", "dataset", "window", "object_key",
                                    "stream_summary", "process_return_code"}
                     and set(outer) == {"artifact", "dataset", "window", "object_key", "process_return_code"}
                     and original(item) == outer["artifact"] and all(item[k] == outer[k]
                     for k in ("dataset", "window", "object_key", "process_return_code")))
            name = item["dataset"]
            if name in windows:
                _require(type(item["process_return_code"]) is int and item["process_return_code"] == 0
                         and item["object_key"] is None)
                windows[name].append(item["window"])
                with ExitStack() as stack:
                    data = reader.file(outer["artifact"], stack, outer["artifact"]["name"])
                    frame = FrameReader(data, scope_sha256=scope.sha256, dataset=name, limits=limits)
                    for row in frame:
                        reader.budget.admit()
                        _require(row["JOURNAL_RECEIVER"] == scope.receiver_windows[0].identity.receiver_name
                                 and scope.receiver_windows[0].start_sequence <= int(row["JOURNAL_SEQUENCE"])
                                 <= scope.receiver_windows[0].end_sequence)
                    _require(frame.summary.to_dict() == item["stream_summary"]
                             and (frame.summary.row_count, frame.summary.rows_sha256)
                             == (streams[name].row_count, streams[name].rows_sha256))
            elif name is not None:
                _require(name in DATASETS and outer["artifact"] == doc["streams"][name]["artifact"])
                if name == "source":
                    _require(type(item["process_return_code"]) is int and item["process_return_code"] == 0)
            else:
                key = item["object_key"]
                _require(type(key) is str and key in trace and key not in recorded
                         and trace[key] == (item["sha256"], item["byte_count"])
                         and item["process_return_code"] is None and item["window"] is None
                         and item["stream_summary"] is None)
                recorded.add(key)
        _require(len(scope.receiver_windows) == 1 and all(value == [scope.receiver_windows[0].to_dict()]
                                                        for value in windows.values()) and recorded == set(trace))

    def _observations(self, reader, doc, files, scope):
        observations = doc.get("authority_observations", {})
        _require(type(observations) is dict and set(observations) <= {"destination", "receiver"})
        validated = []
        def read(value, name):
            _require(value in files.values())
            return reader.read(value, name=name)
        if "destination" in observations:
            _require(self.destination_identity is not None)
            destination = observations["destination"]
            _require(type(destination) is dict and set(destination) == {"history", "mirror"})
            for name in ("history", "mirror"):
                item = destination[name]
                _require(type(item) is dict and set(item) == {"before", "after", "stream_sha256"}
                         and item["stream_sha256"] == doc["streams"][name]["artifact"]["sha256"])
                before, after = [read_destination_identity(read(item[phase], f"destination:{name}:{phase}"),
                    scope, expected=self.destination_identity) for phase in ("before", "after")]
                validate_destination_identity_pair(before, after, scope=scope, expected=self.destination_identity)
                summary = doc["streams"][name]["summary"]
                stamp = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
                _require(stamp(before.completed_at) <= stamp(summary["started_at"])
                         <= stamp(summary["completed_at"]) <= stamp(after.started_at))
            validated.append("destination_identity")
        if "receiver" in observations:
            _require(self.receiver_config is not None)
            item = observations["receiver"]
            _require(type(item) is dict and set(item) == {"before", "after"})
            before, after = [read_receiver_observation(read(item[phase], "receiver:" + phase),
                scope=scope, expected_config=self.receiver_config) for phase in ("before", "after")]
            validate_receiver_pair(scope, before, after, expected_config=self.receiver_config)
            validated.append("receiver_sampled_ranges")
        return validated
