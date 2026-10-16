"""Execute and durably archive native collection without admitting its proof.

Synchronous storage and destination calls require an outer supervised deadline.
The configuration fence does not establish a transactional or physical cut.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import time
from typing import Callable, Mapping

from quadringent.object_store import _validate_object_key
from quadringent.qualification.verification_collect import (
    ProcessSpec,
    WindowProcess,
    collect_verification,
)
from quadringent.qualification.verification_guard import collect_native_guards
from quadringent.qualification.verification_identity import (
    DestinationIdentityExpectation, observe_destination_identity, validate_destination_identity_pair,
)
from quadringent.qualification.verification_receivers import (
    ReceiverObservationConfig, observe_receiver, validate_receiver_pair,
)
from quadringent.qualification.verification_native import (
    load_native_copy_inputs,
    native_journal_processes,
)
from quadringent.qualification.verification_stored import StoredLimits
from quadringent.qualification.verification_stream import StreamLimits
from quadringent.storage_backend import StorageBackend

from .native_artifacts import ArchiveLimits, ArtifactArchive
from .native_inventory import InventoryLimits, StoredRawInventory
from .native_resolution import PreparationResolver, ResolutionLimits
from .native_verifications import NativeArtifact, NativePreparation


class NativeCollectionError(ValueError):
    """A collection could not be completed; input values are never included."""


@dataclass(frozen=True)
class CollectionLimits:
    max_seconds: int = 3600
    streams: StreamLimits = StreamLimits()
    stored: StoredLimits = StoredLimits()
    archive: ArchiveLimits = ArchiveLimits()
    resolution: ResolutionLimits = ResolutionLimits()
    inventory: InventoryLimits = InventoryLimits()

    def __post_init__(self):
        if type(self.max_seconds) is not int or not 1 <= self.max_seconds <= 7200:
            raise ValueError("invalid_native_collection_limits")
        for value, expected in (
            (self.streams, StreamLimits),
            (self.stored, StoredLimits),
            (self.archive, ArchiveLimits),
            (self.resolution, ResolutionLimits),
            (self.inventory, InventoryLimits),
        ):
            if type(value) is not expected:
                raise ValueError("invalid_native_collection_limits")


@dataclass(frozen=True)
class NativeCollectionResult:
    _report: dict = field(repr=False)
    artifacts: tuple[NativeArtifact, ...]
    manifest: NativeArtifact
    native_admission: str = "not_evaluated"
    native_cdc_qualified: bool = False

    @property
    def report(self):
        return deepcopy(self._report)


class _Budget:
    def __init__(self, seconds, current):
        self.started = self.previous = time.monotonic()
        self.seconds, self.current = seconds, current

    def remaining(self):
        now = time.monotonic()
        if not math.isfinite(now) or now < self.previous:
            raise ValueError("collection_deadline")
        self.previous = now
        result = math.floor(self.seconds - (now - self.started))
        if result < 1:
            raise ValueError("collection_deadline")
        return result

    def admit(self):
        self.remaining()
        if self.current() is not True:
            raise ValueError("configuration_superseded")
        self.remaining()
        return True

    def call(self, function, *args, **kwargs):
        self.admit()
        result = function(*args, **kwargs)
        self.admit()
        return result

    def bounded(self, limits):
        return replace(limits, max_seconds=min(limits.max_seconds, self.remaining()))


class _FencedStore:
    def __init__(self, store, budget, prefix=""):
        self.store, self.budget, self.prefix = store, budget, prefix

    def get_bounded(self, key, maximum):
        _validate_object_key(key)
        return self.budget.call(self.store.get_bounded, self.prefix + key, maximum)


def _new_directory(directory):
    path = Path(directory).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("unsafe_collection_directory")
    path.mkdir(mode=0o700)
    created = os.lstat(path)
    try:
        os.chmod(path, 0o700)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            path.rmdir()
        raise
    return path


def _private_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode(value):
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _pinned(store, reference, maximum):
    content = store.get_bounded(reference.key, maximum)
    if type(content) is not bytes or hashlib.sha256(content).hexdigest() != reference.sha256:
        raise ValueError("original_changed")
    return content


class StoredNativeCollector:
    def __init__(
        self,
        storage: StorageBackend,
        raw_prefix_root: str,
        org_id: str,
        *,
        java: str,
        classpath: str,
        credentials: bytes,
        environment: Mapping[str, str],
        source_id: str,
        destination_id: str,
        connection_factory: Callable,
        destination_identity: DestinationIdentityExpectation | None = None,
        receiver_config: ReceiverObservationConfig | None = None,
        limits: CollectionLimits = CollectionLimits(),
    ):
        try:
            if (type(storage) is not StorageBackend or type(limits) is not CollectionLimits
                    or destination_identity is not None and type(destination_identity) is not DestinationIdentityExpectation):
                raise ValueError()
            if (receiver_config is not None and (type(receiver_config) is not ReceiverObservationConfig
                    or (receiver_config.java, receiver_config.classpath) != (java, classpath))):
                raise ValueError()
            _validate_object_key(raw_prefix_root)
            for value in (org_id, source_id, destination_id):
                if type(value) is not str or not 1 <= len(value) <= 256 or value.strip() != value:
                    raise ValueError()
            if (
                type(java) is not str
                or not Path(java).is_absolute()
                or ".." in Path(java).parts
                or type(classpath) is not str
                or not 1 <= len(classpath) <= 8192
                or any(not p or not Path(p).is_absolute() or ".." in Path(p).parts for p in classpath.split(os.pathsep))
                or not callable(connection_factory)
            ):
                raise ValueError()
            validated = ProcessSpec((java,), credentials, environment)
            text = credentials.decode("utf-8")
            parts = text.split("\n")
            if (
                len(parts) != 4
                or parts[-1]
                or any(not p for p in parts[:3])
                or "\r" in text
                or "\0" in text
                or len(parts[0]) > 253
                or len(parts[1]) > 128
                or len(parts[2]) > 4096
            ):
                raise ValueError()
            self.storage, self.raw_prefix_root, self.org_id = storage, raw_prefix_root, org_id
            self.java, self.classpath = java, classpath
            self._credentials, self._environment = validated.input_bytes, validated.environment
            self._host, self._user = parts[:2]
            self.source_id, self.destination_id = source_id, destination_id
            self._connection_factory, self.limits = connection_factory, limits
            self._destination_identity = destination_identity
            self._receiver_config = receiver_config
        except Exception:
            raise NativeCollectionError("invalid_native_collection_configuration") from None

    def _processes(self, scope, seconds):
        limits = self.limits.streams
        journal = scope.copy_boundary.receiver_identity
        command = (
            self.java,
            "-cp",
            self.classpath,
            "io.quadringent.as400.NativeVerificationSourceExport",
            scope.source_library,
            scope.source_table,
            journal.journal_library,
            journal.journal_name,
            ",".join(scope.primary_key),
            ",".join(c.name for c in scope.columns),
            scope.sha256,
            str(limits.max_rows),
            str(limits.max_bytes),
        )
        source = ProcessSpec(command, self._credentials, self._environment)
        positions = []
        for window in scope.receiver_windows:
            identity = window.identity
            if window.start_sequence > 10**21 - 1 or window.end_sequence > 10**21 - 1:
                raise ValueError("unsupported_native_sequence")
            command = (
                self.java,
                "-cp",
                self.classpath,
                "io.quadringent.as400.NativeVerificationPositionsExport",
                identity.journal_library,
                identity.journal_name,
                identity.receiver_library,
                identity.receiver_name,
                scope.source_library,
                scope.source_table,
                str(window.start_sequence),
                str(window.end_sequence),
                scope.sha256,
                str(limits.max_rows),
                str(limits.max_bytes),
                str(limits.max_line_bytes),
                str(min(seconds, limits.max_seconds, 900)),
            )
            positions.append(WindowProcess(window, ProcessSpec(command, self._credentials, self._environment)))
        return source, tuple(positions)

    def collect(
        self, prepared: NativePreparation, *, context: dict, directory: Path, check_current: Callable[[], bool]
    ) -> NativeCollectionResult:
        root = identity = None
        success = False
        try:
            if type(prepared) is not NativePreparation or type(context) is not dict or not callable(check_current):
                raise ValueError("invalid_collection")
            context = deepcopy(context)
            if (context["org_id"], context["source_id"], context["destination_id"]) != (
                self.org_id,
                self.source_id,
                self.destination_id,
            ) or (context["source"]["ibmi_host"], context["source"]["ibmi_user"]) != (self._host, self._user):
                raise ValueError("collection_context_changed")
            budget = _Budget(self.limits.max_seconds, check_current)
            budget.admit()
            resolver = PreparationResolver(
                self.storage, self.raw_prefix_root, self.org_id, limits=budget.bounded(self.limits.resolution)
            )
            resolved = budget.call(resolver.resolve, prepared, context=context, check_current=budget.admit)
            if (
                resolved.org_id,
                resolved.source_id,
                resolved.destination_id,
                resolved.raw_prefix_root,
                resolved.backend,
                resolved.raw_bucket,
            ) != (
                self.org_id,
                self.source_id,
                self.destination_id,
                self.raw_prefix_root,
                self.storage.kind,
                self.storage.raw_bucket,
            ) or resolved.scope != prepared.scope:
                raise ValueError("resolved_context_changed")
            archive = ArtifactArchive(
                self.storage, self.org_id, check_current=budget.admit, limits=budget.bounded(self.limits.archive)
            )
            inventory = StoredRawInventory(
                self.storage,
                self.raw_prefix_root,
                self.org_id,
                archive=archive,
                limits=budget.bounded(self.limits.inventory),
            )
            captured = budget.call(inventory.capture, resolved, check_current=budget.admit)
            store = _FencedStore(captured.store, budget)
            original_store = _FencedStore(resolved.store, budget)
            evidence = _pinned(original_store, resolved.initial_copy, 65536)
            copy = budget.call(
                load_native_copy_inputs,
                _FencedStore(resolved.store, budget, "receiver-authority-v1/"),
                evidence,
                expected_sha256=resolved.initial_copy.sha256,
                evidence_key=resolved.initial_copy.key,
                scope=resolved.scope,
            )
            budget.admit()
            root = _new_directory(directory)
            info = root.stat()
            identity = info.st_dev, info.st_ino
            native_dir = _new_directory(root / "native-inputs")
            receiver_before = receiver_after = None
            if self._receiver_config is not None:
                receiver_before = budget.call(observe_receiver, resolved.scope, phase="before",
                    directory=native_dir / "receiver-before", config=self._receiver_config,
                    input_bytes=self._credentials, check_current=budget.admit)
            bundle, guard_path = native_dir / "descriptor.bundle", native_dir / "journal-guard.json"
            budget.call(_private_file, bundle, copy.bundle)
            source, positions = self._processes(resolved.scope, budget.remaining())
            journal = native_journal_processes(
                copy,
                resolved.scope,
                java=self.java,
                classpath=self.classpath,
                credentials=self._credentials,
                environment=self._environment,
                bundle_path=bundle,
                guard_receipt_path=guard_path,
                max_seconds=min(900, budget.remaining(), self.limits.streams.max_seconds),
            )
            budget.call(resolved.verify)
            budget.call(captured.verify)
            destination_observations = {}

            def connection_factory():
                budget.admit()
                connection = self._connection_factory()
                try:
                    budget.admit()
                    if self._destination_identity is None:
                        return connection
                    name = ("history", "mirror")[len(destination_observations)]
                    before = observe_destination_identity(connection, resolved.scope,
                        expected=self._destination_identity, check_current=budget.admit,
                        max_seconds=min(60, budget.remaining()))
                    destination_observations[name] = {"before": before}
                    owner = self
                    class ObservedConnection:
                        closed = False

                        def __getattr__(self, attribute):
                            return getattr(connection, attribute)

                        def close(self):
                            if self.closed:
                                return
                            self.closed = True
                            try:
                                after = observe_destination_identity(connection, resolved.scope,
                                    expected=owner._destination_identity, check_current=budget.admit,
                                    max_seconds=min(60, budget.remaining()))
                                validate_destination_identity_pair(before, after, scope=resolved.scope,
                                                                   expected=owner._destination_identity)
                                destination_observations[name]["after"] = after
                            finally:
                                connection.close()
                    return ObservedConnection()
                except BaseException:
                    connection.close()
                    raise

            result = budget.call(
                collect_verification,
                root / "seven-streams",
                resolved.scope,
                source=source,
                positions=positions,
                journal=journal,
                store=store,
                raw_prefix_root=self.raw_prefix_root,
                initial_copy=resolved.initial_copy,
                copy_intent=resolved.copy_intent,
                raw_read_set=captured.read_set,
                connection_factory=connection_factory,
                check_current=budget.admit,
                limits=budget.bounded(self.limits.streams),
                stored_limits=self.limits.stored,
                max_seconds=budget.remaining(),
            )
            report_bytes = _encode(result.report)
            if (
                len(report_bytes) != result.report_reference.byte_count
                or hashlib.sha256(report_bytes).hexdigest() != result.report_reference.sha256
            ):
                raise ValueError("comparison_changed")
            guards = budget.call(
                collect_native_guards,
                copy,
                resolved.scope,
                journal[0].process,
                guard_receipt_path=guard_path,
                directory=native_dir / "after-all-streams",
                check_current=budget.admit,
                max_seconds=min(60, budget.remaining()),
            )
            if self._receiver_config is not None:
                receiver_after = budget.call(observe_receiver, resolved.scope, phase="after",
                    directory=native_dir / "receiver-after", config=self._receiver_config,
                    input_bytes=self._credentials, check_current=budget.admit)
                budget.call(validate_receiver_pair, resolved.scope, receiver_before, receiver_after,
                            expected_config=self._receiver_config)
            budget.call(resolved.verify)
            budget.call(captured.verify)
            retained = {}

            def retain(name, path, digest, count):
                path = Path(path)
                if path in retained:
                    artifact = retained[path]
                    if (artifact.sha256, artifact.byte_count) != (digest, count):
                        raise ValueError("original_changed")
                    return artifact
                artifact = budget.call(archive.retain_file, name, path, expected_sha256=digest, byte_count=count)
                retained[path] = artifact
                return artifact

            streams = {
                name: {
                    "artifact": retain(
                        "stream:" + name, reference.path, reference.summary.sha256, reference.summary.byte_count
                    ).to_dict(),
                    "summary": reference.summary.to_dict(),
                }
                for name, reference in result.streams.items()
            }
            originals = []
            for index, reference in enumerate(result.originals):
                artifact = retain(f"original:{index:06d}", reference.path, reference.sha256, reference.byte_count)
                originals.append(
                    {
                        "artifact": artifact.to_dict(),
                        "dataset": reference.dataset,
                        "window": reference.window.to_dict() if reference.window else None,
                        "object_key": reference.object_key,
                        "process_return_code": reference.process_return_code,
                    }
                )
            comparison = retain(
                "comparison",
                result.report_reference.path,
                result.report_reference.sha256,
                result.report_reference.byte_count,
            )
            collection = retain(
                "collection",
                result.collection_reference.path,
                result.collection_reference.sha256,
                result.collection_reference.byte_count,
            )
            retained_bundle = retain("native_bundle", bundle, copy.bundle_sha256, len(copy.bundle))
            guard_refs = {}
            for name in ("journal_original", "final_original", "stdout_original"):
                reference = getattr(guards, name)
                guard_refs[name] = retain(
                    "guard:" + name, reference.path, reference.sha256, reference.byte_count
                ).to_dict()
            authority_observations = {}
            if receiver_before is not None:
                authority_observations["receiver"] = {
                    phase: retain("receiver:" + phase, value.original.path, value.original.sha256,
                                  value.original.byte_count).to_dict()
                    for phase, value in (("before", receiver_before), ("after", receiver_after))}
            if self._destination_identity is not None:
                if set(destination_observations) != {"history", "mirror"}:
                    raise ValueError("destination_identity_incomplete")
                destination_refs = {}
                for name, observations in destination_observations.items():
                    if set(observations) != {"before", "after"}:
                        raise ValueError("destination_identity_incomplete")
                    refs = {}
                    for phase, observation in observations.items():
                        path = native_dir / f"destination-{name}-{phase}.json"
                        budget.call(_private_file, path, observation.original_bytes)
                        refs[phase] = retain(f"destination:{name}:{phase}", path,
                                             observation.sha256, observation.byte_count).to_dict()
                    destination_refs[name] = dict(refs, stream_sha256=result.streams[name].summary.sha256)
                authority_observations["destination"] = destination_refs
            artifacts = tuple(resolved.artifacts) + (captured.inventory_artifact,) + tuple(retained.values())
            if len({value.name for value in artifacts}) != len(artifacts):
                raise ValueError("artifact_names_conflict")
            budget.call(archive.verify)
            budget.call(resolved.verify)
            budget.call(captured.verify)
            manifest_document = {
                    "format_version": "quadringent-native-server-collection-v2" if authority_observations else "quadringent-native-server-collection-v1",
                    "scope_sha256": resolved.scope.sha256,
                    "context_sha256": resolved.context_sha256,
                    "source_id": self.source_id,
                    "destination_id": self.destination_id,
                    "native_admission": "not_evaluated",
                    "native_cdc_qualified": False,
                    "transaction_consistency_verified": False,
                    "physical_inventory_admission": "not_evaluated",
                    "preparation": [value.to_dict() for value in resolved.artifacts],
                    "raw_inventory": captured.inventory_artifact.to_dict(),
                    "streams": streams,
                    "originals": originals,
                    "comparison": comparison.to_dict(),
                    "collection": collection.to_dict(),
                    "native_bundle": retained_bundle.to_dict(),
                    "guards": guard_refs,
                    "guard_metadata": guards.metadata,
                    "files": [
                        {"relative_path": path.relative_to(root).as_posix(), "artifact": value.to_dict()}
                        for path, value in retained.items()
                    ],
                }
            if authority_observations:
                manifest_document["authority_observations"] = authority_observations
            manifest_bytes = _encode(manifest_document)
            manifest = budget.call(archive.retain_bytes, "collection_manifest", manifest_bytes)
            budget.call(archive.verify)
            budget.call(resolved.verify)
            budget.call(captured.verify)
            budget.admit()
            success = True
            return NativeCollectionResult(deepcopy(result.report), artifacts + (manifest,), manifest)
        except Exception:
            raise NativeCollectionError("native_collection_failed") from None
        finally:
            if not success and root is not None and identity is not None:
                try:
                    current = os.lstat(root)
                    if (
                        stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino) == identity
                        and not any(path.is_symlink() for path in root.parents)
                    ):
                        shutil.rmtree(root)
                except OSError:
                    pass
