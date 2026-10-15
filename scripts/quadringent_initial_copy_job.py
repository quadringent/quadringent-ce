#!/usr/bin/env python3
"Publish a JDBC initial snapshot and its durable copy evidence."

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from quadringent.capture_binding import CaptureBinding
from quadringent.checkpoint import _decode_checkpoint_json
from quadringent.continuous import ReceiverPlanningError, require_readable_receiver
from quadringent.java_catalog import JavaReceiverCatalog
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef, copy_intent_key

READER_CLASS = "io.quadringent.as400.ReadOnlyTableSnapshot"
SNAPSHOT_SUMMARY = re.compile(r"\brows=(\d+)\b")
MAX_STDERR_TAIL = 4000


class InitialCopyJobError(RuntimeError):
    """Configuration invalide ou étape en échec — le Job doit s'arrêter (fail-closed)."""


def _required(environ: dict, name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise InitialCopyJobError(f"{name} is required")
    return value


@dataclass(frozen=True)
class InitialCopyConfig:
    """Configuration résolue depuis l'environnement, avant tout I/O réseau."""

    java: str
    java_classpath: str
    ibmi_host: str
    ibmi_user: str
    ibmi_password: str
    schema: str
    table: str
    run_id: str
    output_dir: Path
    storage_backend: str
    raw_bucket: str
    raw_prefix: str
    receiver_name: str
    receiver_library: str
    bootstrap_sequence: int
    observed_at: str
    evidence_key: str
    pipeline_id: str
    table_id: str
    boundary: JournalBoundary

    @classmethod
    def from_environment(cls, environ: dict) -> "InitialCopyConfig":
        try:
            boundary = JournalBoundary.from_dict(_decode_checkpoint_json(_required(environ, "AS400_BOUNDARY_JSON")))
            binding = CaptureBinding.from_dict(_decode_checkpoint_json(_required(environ, "AS400_CAPTURE_BINDING_JSON")))
            boundary.require_bound()
            if boundary.capture_binding != binding:
                raise ValueError("copy boundary differs from its original capture binding")
            backend = (environ.get("QUADRINGENT_STORAGE_BACKEND") or "aws").strip().lower()
            from quadringent.storage_backend import registry_authority

            authority = registry_authority(backend, _required(environ, "AS400_RAW_BUCKET"))
            if binding.registry_authority != authority or binding.source_system != "ibmi":
                raise ValueError("copy binding differs from its storage or event authority")
            expected = {
                "AS400_BOOTSTRAP_RECEIVER": boundary.receiver_name,
                "AS400_BOOTSTRAP_RECEIVER_LIBRARY": boundary.receiver_library,
                "AS400_BOOTSTRAP_SEQUENCE": str(boundary.last_sequence),
                "AS400_BOOTSTRAP_OBSERVED_AT": boundary.observed_at.isoformat(),
            }
            if any(environ.get(name) != value for name, value in expected.items()):
                raise ValueError("copy bootstrap fields differ from the bound boundary")
        except (TypeError, ValueError) as error:
            raise InitialCopyJobError("a complete matching bound copy handoff is required") from error
        return cls(
            java=environ.get("AS400_JAVA", "java"),
            java_classpath=_required(environ, "AS400_JAVA_CLASSPATH"),
            ibmi_host=_required(environ, "ISERIES_HOST"),
            ibmi_user=_required(environ, "ISERIES_USER"),
            ibmi_password=_required(environ, "ISERIES_PASSWORD"),
            schema=_required(environ, "ISERIES_SCHEMA"),
            table=_required(environ, "ISERIES_TABLE"),
            run_id=_required(environ, "AS400_SNAPSHOT_RUN_ID"),
            output_dir=Path(_required(environ, "AS400_SNAPSHOT_OUTPUT_DIR")),
            storage_backend=backend,
            raw_bucket=_required(environ, "AS400_RAW_BUCKET"),
            raw_prefix=_required(environ, "AS400_RAW_PREFIX"),
            receiver_name=boundary.receiver_name,
            receiver_library=boundary.receiver_library,
            bootstrap_sequence=boundary.last_sequence,
            observed_at=boundary.observed_at.isoformat(),
            evidence_key=_required(environ, "AS400_EVIDENCE_KEY"),
            pipeline_id=_required(environ, "AS400_PIPELINE_ID"),
            table_id=_required(environ, "AS400_TABLE_ID"),
            boundary=boundary,
        )


def snapshot_subprocess_env(config: InitialCopyConfig, base_environ: dict) -> dict:
    """Environnement du sous-processus Java — jamais le mot de passe ailleurs.

    Reprend l'environnement du pod (``AS400_TLS``/``AS400_TLS_CA_FILE``
    posés par ``_with_ca_mount`` y transitent tels quels) et pose les clés
    que ``ReadOnlyTableSnapshot.Settings.fromEnvironment`` (Java) exige,
    notamment ``AS400_RAW_DIRECTORY`` — nom différent de la variable posée
    par le manifest (``AS400_SNAPSHOT_OUTPUT_DIR``), traduit ici, une seule
    fois, plutôt que dans deux endroits.
    """

    env = dict(base_environ)
    env["AS400_RAW_DIRECTORY"] = str(config.output_dir)
    env["ISERIES_PASSWORD"] = config.ibmi_password
    return env


def run_snapshot(config: InitialCopyConfig, *, base_environ: dict, runner=subprocess.run) -> int:
    """Lance ``ReadOnlyTableSnapshot`` en sous-processus, renvoie les lignes copiées."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    env = snapshot_subprocess_env(config, base_environ)
    completed = runner(
        [config.java, "-cp", config.java_classpath, READER_CLASS],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or "")[-MAX_STDERR_TAIL:]
        raise InitialCopyJobError(f"JDBC snapshot failed (exit code {completed.returncode}) : {tail}")
    match = SNAPSHOT_SUMMARY.search(completed.stdout or "")
    if not match:
        raise InitialCopyJobError("snapshot summary not found in reader output")
    return int(match.group(1))


def publish_snapshot(config: InitialCopyConfig, *, base_environ: dict) -> list[dict[str, str]]:
    """Publie ``config.output_dir`` via les fonctions réelles de
    ``as400_snapshot_publish`` — importées, jamais un sous-processus
    ``docker`` (voir ``quadringent_fleet_chunked_snapshot.py`` pour le
    contre-exemple : inadapté à un Job Kubernetes).

    Renvoie les clés publiées (``payload_key``/``manifest_key`` par lot) :
    portées ensuite par la preuve (``build_evidence``), c'est ce qui permet
    au chargeur de charger l'instantané sans capacité de listage générique
    sur ``ObjectStore`` (voir ``as400_snapshot_publish.publish_snapshot_
    batches``)."""

    bind_local_snapshots(config)
    import as400_snapshot_publish as publish

    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(base_environ)
        os.environ["AS400_RAW_DIRECTORY"] = str(config.output_dir)
        os.environ["QUADRINGENT_STORAGE_BACKEND"] = config.storage_backend
        os.environ["ISERIES_TABLE"] = config.table
        from quadringent.storage_backend import backend_from_environment

        backend = backend_from_environment(os.environ)
        store = None
        client = None
        if backend == "local":
            from quadringent.storage_backend import StorageBackend

            store = StorageBackend.from_environment(os.environ).object_store("")
        elif backend == "gcs":
            from quadringent.gcs_backend import GcsObjectStore

            store = GcsObjectStore(config.raw_bucket)
        else:
            import boto3

            client = boto3.client("s3")
        return publish.publish_snapshot_batches(
            root=config.output_dir,
            bucket=config.raw_bucket,
            prefix=config.raw_prefix,
            table=config.table,
            backend=backend,
            store=store,
            client=client,
        )
    finally:
        os.environ.clear()
        os.environ.update(previous)


def bind_local_snapshots(config: InitialCopyConfig) -> None:
    from quadringent.raw import bind_snapshot_batch

    point = config.boundary.require_bound()
    for payload_path in sorted(config.output_dir.glob("batch-*.jsonl")):
        manifest_path = payload_path.with_suffix(".manifest.json")
        payload = payload_path.read_bytes()
        content = bind_snapshot_batch(manifest_path.read_bytes(), payload,
                                      binding=config.boundary.capture_binding, boundary=point,
                                      pipeline_id=config.pipeline_id, table_id=config.table_id, run_id=config.run_id)
        batch_id = json.loads(content)["batch_id"]
        if type(batch_id) is not str or re.fullmatch(r"[0-9a-f]{32}", batch_id) is None:
            raise InitialCopyJobError("bound snapshot batch identity is invalid")
        new_payload = config.output_dir / f"batch-{batch_id}.jsonl"
        new_manifest = config.output_dir / f"batch-{batch_id}.manifest.json"
        for path, body in ((new_payload, payload), (new_manifest, content)):
            if path.exists() and path.read_bytes() != body:
                raise InitialCopyJobError("bound snapshot artifact collision")
            if not path.exists():
                path.write_bytes(body)
        if payload_path != new_payload:
            payload_path.unlink()
        if manifest_path != new_manifest:
            manifest_path.unlink()


def validate_original_binding(config: InitialCopyConfig) -> None:
    from quadringent.storage_backend import StorageBackend

    binding = config.boundary.capture_binding
    storage = StorageBackend(config.storage_backend, config.raw_bucket, "")
    registry = ReceiverNameRegistry(storage.object_store("receiver-authority-v1"),
                                    authority=binding.registry_authority,
                                    source_system=binding.source_system, journal=binding.journal)
    if registry.load_binding(binding.scope_id) != binding:
        raise InitialCopyJobError("original capture binding is absent or differs")
    registry.require(binding, binding.bootstrap)
    registry.require(binding, config.boundary.require_bound())
    try:
        intent = _decode_checkpoint_json(storage.object_store("receiver-authority-v1").get_bounded(
            copy_intent_key(config.pipeline_id, config.table_id, config.run_id), 65_536))
        expected = {"format_version": "quadringent-copy-intent-v1", "pipeline_id": config.pipeline_id,
                    "table_id": config.table_id, "run_id": config.run_id, "boundary": config.boundary.to_dict()}
        if intent != expected:
            raise ValueError("copy intent differs from the original admitted run")
    except (FileNotFoundError, ValueError) as error:
        raise InitialCopyJobError("original copy intention is absent or differs") from error


def validate_native_boundary(config: InitialCopyConfig, *, base_environ: dict, catalog_factory=None) -> None:
    identity = config.boundary.require_bound().receiver_identity
    factory = catalog_factory or JavaReceiverCatalog
    catalog = factory(java=config.java, classpath=config.java_classpath, host=config.ibmi_host,
                      user=config.ibmi_user, password=config.ibmi_password,
                      journal_library=identity.journal_library, journal_name=identity.journal_name,
                      ca_file=base_environ.get("AS400_TLS_CA_FILE"))
    identity_args = dict(journal_library=identity.journal_library, journal_name=identity.journal_name,
                         receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
                         asp_number=1)
    before = catalog.identity(**identity_args)
    receivers = [item for item in catalog.snapshot(required_receiver=identity.receiver_name)
                 if (item.receiver_library, item.receiver) == (identity.receiver_library, identity.receiver_name)]
    after = catalog.identity(**identity_args)
    if before != identity or after != identity or len(receivers) != 1:
        raise InitialCopyJobError("native receiver identity changed during initial copy")
    receiver = receivers[0]
    try:
        require_readable_receiver(receiver)
    except ReceiverPlanningError as error:
        raise InitialCopyJobError("original copy receiver is not readable") from error
    first = receiver.first_sequence
    last = receiver.last_sequence
    if (type(first) is not int or first < 1
            or (last is not None and (type(last) is not int or last < first - 1))
            or config.bootstrap_sequence < first - 1
            or config.bootstrap_sequence > (last if last is not None else first - 1)):
        raise InitialCopyJobError("original copy boundary is no longer covered by native receiver metadata")


def build_evidence(
    config: InitialCopyConfig, *, rows_copied: int, completed_at: datetime,
    snapshot_batches: list[dict[str, str]] = (),
) -> dict:
    """Même forme que ``v2/executor/evidence.py::InitialCopyEvidence.to_dict()`` —
    vérifié par un test qui la relit avec ``InitialCopyEvidence.from_dict``.
    Le contrôle plane n'écrit jamais cette preuve, seulement ce Job.

    ``snapshot_batches`` (clés publiées par ``publish_snapshot``) permet au
    chargeur de charger l'instantané avant les événements sans capacité de
    listage générique sur ``ObjectStore``."""

    config.boundary.require_bound()
    return InitialCopyEvidence(
        config.pipeline_id, config.table_id, config.run_id, config.boundary,
        rows_copied, completed_at,
        tuple(SnapshotBatchRef.from_dict(batch) for batch in snapshot_batches),
    ).to_dict()


def write_evidence(config: InitialCopyConfig, evidence: dict) -> None:
    """Écrit la preuve à la clé attendue — même magasin racine (préfixe vide)
    que ``EvidenceReader`` côté control plane (``entrypoint.py::
    _storage_backend_object_store``) : ``evidence_key`` porte déjà le chemin
    complet."""

    from quadringent.storage_backend import StorageBackend

    storage = StorageBackend(kind=config.storage_backend, raw_bucket=config.raw_bucket, state_location="")
    store = storage.object_store("")
    body = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    store.put_once(config.evidence_key, body)


def main(argv: list[str] | None = None, *, environ: dict | None = None, now=None, catalog_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and validate configuration without starting a snapshot or publishing anything",
    )
    args = parser.parse_args(argv)
    base_environ = dict(os.environ if environ is None else environ)
    config = InitialCopyConfig.from_environment(base_environ)
    if args.dry_run:
        print(json.dumps({"event": "initial_copy_dry_run", "table": f"{config.schema}.{config.table}"}))
        return 0
    validate_original_binding(config)
    validate_native_boundary(config, base_environ=base_environ, catalog_factory=catalog_factory)
    rows_copied = run_snapshot(config, base_environ=base_environ)
    validate_native_boundary(config, base_environ=base_environ, catalog_factory=catalog_factory)
    validate_original_binding(config)
    snapshot_batches = publish_snapshot(config, base_environ=base_environ)
    completed_at = now() if now is not None else datetime.now(timezone.utc)
    evidence = build_evidence(
        config, rows_copied=rows_copied, completed_at=completed_at, snapshot_batches=snapshot_batches
    )
    write_evidence(config, evidence)
    print(
        json.dumps(
            {
                "event": "initial_copy_completed",
                "table_id": config.table_id,
                "run_id": config.run_id,
                "rows_copied": rows_copied,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
