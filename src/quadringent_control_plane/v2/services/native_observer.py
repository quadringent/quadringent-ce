"""Installed read-only collection/replay; this port cannot grant native admission."""

from __future__ import annotations

from copy import deepcopy
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from sqlalchemy import select

from quadringent.storage_backend import StorageBackend
from .native_context import canonical
from .native_observer_process import NativeObserverProcessError, bounded_call, run_worker
from .native_verifications import NativeArtifact, NativePreparation
from .. import schema

_ENVIRONMENT = {
    "PATH",
    "JAVA_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "HOME",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_EC2_METADATA_DISABLED",
}


def _require(value):
    if not value:
        raise ValueError("native_observation_configuration")


def installed_runtime(values):
    """Resolve server paths without connecting to a source, store or destination."""
    if sys.platform != "linux":
        return None
    java = shutil.which(values.get("AS400_JAVA") or "java", path=values.get("PATH") or os.environ.get("PATH"))
    classpath = (values.get("AS400_JAVA_CLASSPATH") or "").strip()
    if not java or not classpath:
        return None
    paths = []
    for part in classpath.split(os.pathsep):
        _require(part and Path(part).is_absolute() and ".." not in Path(part).parts)
        expanded = sorted(glob.glob(part)) if "*" in part else [part]
        _require(expanded and len(expanded) <= 64)
        paths.extend(str(Path(p).resolve(strict=True)) for p in expanded)
    java = str(Path(java).resolve(strict=True))
    files = {}
    total = 0
    for name in dict.fromkeys([java, *paths]):
        path = Path(name)
        _require(path.is_file() and path.stat().st_size <= 256 * 1024**2)
        total += path.stat().st_size
        _require(total <= 1024**3)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"java": java, "classpath": os.pathsep.join(paths), "files": files}


def _runtime_current(runtime):
    for name, digest in runtime["files"].items():
        path = Path(name)
        _require(
            path.is_file()
            and path.stat().st_size <= 256 * 1024**2
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        )


def _safe_environment(directory):
    values = {k: v for k, v in os.environ.items() if k in _ENVIRONMENT}
    values.update(PYTHONPATH=str(Path(__file__).resolve().parents[3]), PYTHONUNBUFFERED="1", TMPDIR=directory)
    return values


def _decode_result(value):
    from .native_collection import NativeCollectionResult
    from .native_replay import NativeArchiveReplay

    _require(type(value) is dict and set(value) == {"collection", "replay"})
    collection, replay = value["collection"], value["replay"]
    _require(
        type(collection) is dict
        and set(collection) == {"report", "artifacts", "manifest"}
        and type(replay) is dict
        and set(replay)
        == {
            "comparison",
            "streams_replayed",
            "missing_authorities",
            "validated_observations",
            "state",
            "native_admission",
            "native_cdc_qualified",
            "collection_manifest",
            "scope_sha256",
            "context_sha256",
        }
    )
    result = NativeCollectionResult(
        collection["report"],
        tuple(NativeArtifact(**v) for v in collection["artifacts"]),
        NativeArtifact(**collection["manifest"]),
    )
    checked = NativeArchiveReplay(
        replay["comparison"],
        replay["streams_replayed"],
        tuple(replay["missing_authorities"]),
        tuple(replay["validated_observations"]),
        replay["state"],
        replay["native_admission"],
        replay["native_cdc_qualified"],
        NativeArtifact(**replay["collection_manifest"]),
        replay["scope_sha256"],
        replay["context_sha256"],
    )
    return result, checked


class InstalledNativeObserver:
    def __init__(self, engine, secret_box, *, storage, root, org_id, runtime, producer_revision, max_seconds=3600):
        _require(type(storage) is StorageBackend and type(max_seconds) is int and 60 <= max_seconds <= 7100)
        self.engine, self.box, self.storage, self.root = engine, secret_box, storage, root
        self.org_id, self.runtime, self.revision, self.seconds = (
            org_id,
            deepcopy(runtime),
            producer_revision,
            max_seconds,
        )

    def _configuration(self, context):
        _require(context["org_id"] == self.org_id and context["source"]["tls_trust"] in {None, "system", "pinned"})
        with self.engine.connect() as connection:
            source = (
                connection.execute(
                    select(schema.sources.c.secret_ciphertext, schema.sources.c.tls_pinned_pem).where(
                        schema.sources.c.org_id == self.org_id, schema.sources.c.id == context["source_id"]
                    )
                )
                .mappings()
                .one()
            )
            destination = (
                connection.execute(
                    select(schema.destinations.c.key_pair_ciphertext).where(
                        schema.destinations.c.org_id == self.org_id,
                        schema.destinations.c.id == context["destination_id"],
                    )
                )
                .mappings()
                .one()
            )
        pem = source["tls_pinned_pem"] or ""
        _require(hashlib.sha256(pem.encode()).hexdigest() == context["source"]["tls_pin_sha256"])
        return {
            "password": self.box.decrypt(source["secret_ciphertext"]),
            "private_key": self.box.decrypt(destination["key_pair_ciphertext"]),
            "tls_pinned_pem": pem if context["source"]["tls_trust"] == "pinned" else "",
        }

    def observe(self, prepared, *, context, check_current):
        try:
            _require(type(prepared) is NativePreparation and type(context) is dict and callable(check_current))
            deadline = time.monotonic() + self.seconds

            def configure():
                _require(check_current() is True)
                value = self._configuration(context)
                _require(check_current() is True)
                return value

            secrets = bounded_call(configure, max_seconds=self.seconds)
            with tempfile.TemporaryDirectory(prefix="quadringent-native-observation-") as directory:
                os.chmod(directory, 0o700)
                payload = {
                    "format_version": "quadringent-native-observation-worker-v1",
                    "org_id": self.org_id,
                    "storage": {
                        "kind": self.storage.kind,
                        "raw_bucket": self.storage.raw_bucket,
                        "state_location": self.storage.state_location,
                        "root": self.root,
                    },
                    "prepared": {
                        "scope": prepared.scope.to_dict(),
                        "artifacts": [v.to_dict() for v in prepared.artifacts],
                    },
                    "context": deepcopy(context),
                    "runtime": self.runtime,
                    "producer_revision": self.revision,
                    "max_seconds": max(1, int(deadline - time.monotonic()) - 2),
                    "directory": directory,
                    "secrets": secrets,
                }
                value = run_worker(
                    (sys.executable, "-P", "-u", "-m", __name__),
                    payload,
                    environment=_safe_environment(directory),
                    check_current=check_current,
                    max_seconds=deadline - time.monotonic(),
                )
                _require(bounded_call(check_current, max_seconds=deadline - time.monotonic()) is True)
                collection, replay = _decode_result(value)
                _require(replay.state == "incomplete" and replay.native_admission == "not_evaluated"
                         and replay.native_cdc_qualified is False and type(replay.streams_replayed) is int
                         and replay.streams_replayed == 7 and collection.report == replay.comparison
                         and replay.collection_manifest == collection.manifest
                         and replay.scope_sha256 == prepared.scope.sha256
                         and collection.report.get("scope_sha256") == prepared.scope.sha256
                         and replay.context_sha256 == hashlib.sha256(canonical(context).encode()).hexdigest())
                _require(set(replay.validated_observations) <= {"native_source_observations", "receiver_sampled_ranges",
                                                               "destination_identity"}
                         and "native_source_observations" in replay.validated_observations
                         and {"platform", "source_provenance", "receiver_chain", "physical_inventory", "stable_cut"}
                         <= set(replay.missing_authorities))
                return collection, replay
        except Exception:
            raise NativeObserverProcessError("native_observation_failed") from None


def collect_and_replay(collector, replayer, prepared, *, context, directory, check_current):
    """Use the retained manifest as the only hand-off between collection and replay."""
    _require(check_current() is True)
    result = collector.collect(
        prepared, context=context, directory=Path(directory) / "collection", check_current=check_current
    )
    replay = replayer.replay(
        prepared, result.manifest, context=context, directory=Path(directory) / "replay", check_current=check_current
    )
    _require(
        check_current() is True
        and result.report == replay.comparison
        and result.manifest == replay.collection_manifest
        and replay.scope_sha256 == prepared.scope.sha256
        and replay.context_sha256 == hashlib.sha256(canonical(context).encode()).hexdigest()
    )
    return result, replay


def _execute(payload, current):
    from quadringent.qualification.verification_contract import VerificationScope
    from quadringent.qualification.verification_receivers import ReceiverObservationConfig
    from .destination_verifier import DestinationVerifierRequest, SnowflakeKeyPairVerifier, warehouse_name_for
    from .native_collection import CollectionLimits, StoredNativeCollector
    from .native_replay import StoredCollectionReplay

    _require(payload["format_version"] == "quadringent-native-observation-worker-v1")
    current()
    _runtime_current(payload["runtime"])
    scope = VerificationScope.from_dict(payload["prepared"]["scope"])
    prepared = NativePreparation(scope, tuple(NativeArtifact(**v) for v in payload["prepared"]["artifacts"]))
    context = payload["context"]
    settings = payload["storage"]
    runtime = payload["runtime"]
    secrets = payload["secrets"]
    storage = StorageBackend(
        kind=settings["kind"], raw_bucket=settings["raw_bucket"], state_location=settings["state_location"]
    )
    directory = Path(payload["directory"])
    _require(directory.is_dir() and not directory.is_symlink())
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "JAVA_HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"}}
    if context["source"]["tls_trust"] == "pinned":
        certificate = directory / "source-ca.pem"
        with certificate.open("x") as stream:
            os.chmod(certificate, 0o600)
            stream.write(secrets["tls_pinned_pem"])
        environment["AS400_TLS_CA_FILE"] = str(certificate)
    credentials = (
        "\n".join((context["source"]["ibmi_host"], context["source"]["ibmi_user"], secrets["password"])) + "\n"
    ).encode()
    target = context["destination"]
    verifier = SnowflakeKeyPairVerifier()
    request = DestinationVerifierRequest(
        target["snowflake_account"],
        target["service_user"],
        target["service_role"],
        warehouse_name_for(target["service_role"]),
        secrets["private_key"],
        target["destination_database"],
        target["destination_schema"],
    )
    receiver = ReceiverObservationConfig(
        runtime["java"], runtime["classpath"], payload["producer_revision"], environment
    )
    seconds = payload["max_seconds"]
    collector = StoredNativeCollector(
        storage,
        settings["root"],
        payload["org_id"],
        java=runtime["java"],
        classpath=runtime["classpath"],
        credentials=credentials,
        environment=environment,
        source_id=context["source_id"],
        destination_id=context["destination_id"],
        connection_factory=lambda: verifier._connect(request),
        receiver_config=receiver,
        limits=CollectionLimits(max_seconds=max(1, seconds // 2)),
    )
    replayer = StoredCollectionReplay(
        storage,
        settings["root"],
        payload["org_id"],
        max_seconds=max(1, seconds // 2),
        receiver_config=receiver,
        java=runtime["java"],
        classpath=runtime["classpath"],
        environment=environment,
    )
    result, replay = collect_and_replay(
        collector, replayer, prepared, context=context, directory=directory, check_current=current
    )
    _runtime_current(runtime)
    current()
    record = {
        "comparison": replay.comparison,
        "streams_replayed": replay.streams_replayed,
        "missing_authorities": list(replay.missing_authorities),
        "validated_observations": list(replay.validated_observations),
        "state": replay.state,
        "native_admission": replay.native_admission,
        "native_cdc_qualified": replay.native_cdc_qualified,
        "collection_manifest": replay.collection_manifest.to_dict(),
        "scope_sha256": replay.scope_sha256,
        "context_sha256": replay.context_sha256,
    }
    return {
        "collection": {
            "report": result.report,
            "artifacts": [v.to_dict() for v in result.artifacts],
            "manifest": result.manifest.to_dict(),
        },
        "replay": record,
    }


def main():
    from .native_observer_process import _decode

    index = 0

    def current():
        nonlocal index
        index += 1
        print(json.dumps({"kind": "fence", "index": index}), flush=True)
        answer = _decode(sys.stdin.buffer.readline(1024))
        _require(answer == {"current": True, "index": index})
        return True

    try:
        raw = sys.stdin.buffer.readline(4 * 1024**2 + 1)
        _require(len(raw) <= 4 * 1024**2 and raw.endswith(b"\n"))
        payload = _decode(raw)
        result = _execute(payload, current)
        print(json.dumps({"kind": "result", "value": result}, separators=(",", ":"), allow_nan=False), flush=True)
        _require(_decode(sys.stdin.buffer.readline(1024)) == {"finish": True})
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
