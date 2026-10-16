"""Single-machine runtime commands for ``quadringent local``.

A host CLI drives plain ``docker run`` of the published images over one shared
data directory (``AS400_LOCAL_DATA_DIR``, storage backend ``local``). This
module is the in-image half: small subcommands that reuse the production
building blocks and print exactly one result line on stdout::

    quadringent_local_result=<json>

Failures exit non-zero with one redacted line on stderr
(``quadringent_local_error=<reason>``). ``fs-probe`` and ``snowflake-verify``
also print their (secret-free) result before exiting 1, so the caller can show
which check failed.

Subcommands
-----------
``init-data``         create the marker and the sub-directories of a fresh data directory
                      (a Docker named volume), owned by the host user; idempotent.
``read-status``       read the capture console snapshot and the copy-evidence row count
                      out of the data directory, read-only, for ``quadringent local status``.
``fs-probe``          prove the data directory offers hard links, flock, atomic
                      rename and directory fsync (``probe_local_filesystem``).
``boundary``          read the attached journal receiver and its native identity,
                      the same sequence as ``boundary_reader.JavaBoundaryReader``.
``admit``             original capture admission and copy intent, the same steps as
                      ``KubernetesPipelineExecutor._start`` up to the copy Job.
``copy``              run ``scripts/quadringent_initial_copy_job.py`` unchanged on the
                      local backend, once per admitted run.
``snowflake-verify``  ``SnowflakeKeyPairVerifier`` against the declared destination.
``exec-capture``      replace this process by the capture reader with the IBM i
                      password in its environment only.
``exec-loader``       replace this process by the destination loader with the
                      Snowflake private key in its environment only.

Secrets are mounted as read-only files (``QUADRINGENT_LOCAL_IBMI_PASSWORD_FILE``,
default ``/run/secrets/ibmi_password``; ``QUADRINGENT_LOCAL_SNOWFLAKE_KEY_FILE``,
default ``/run/secrets/snowflake_key.p8``). They are read here and handed to the
child through its environment: never argv, never a log line.

Non-secret inputs use the variable names the capture, copy and loader scripts
already read (``ISERIES_HOST``, ``ISERIES_USER``, ``AS400_JOURNAL_LIBRARY``,
``AS400_JOURNAL_NAME``, ``AS400_RAW_BUCKET``, ``AS400_LOCAL_DATA_DIR``,
``AS400_JAVA``, ``AS400_JAVA_CLASSPATH``, ``SNOWFLAKE_*`` ...).

Only modules present in the capture image and the control-plane image are
imported at load; ``boundary_reader`` needs SQLAlchemy and is mirrored here
(``read_boundary``) with a parity test instead of being imported.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from quadringent.capture_binding import CaptureBinding
from quadringent.checkpoint import _decode_checkpoint_json
from quadringent.java_catalog import JavaReceiverCatalog
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.storage_backend import (
    LOCAL_DATA_DIR_ENV,
    LOCAL_MARKER_NAME,
    StorageBackend,
    probe_local_filesystem,
    registry_authority,
    require_local_marker,
    validate_local_label,
)

from .boundary import BoundaryError, JournalBoundary, plan_bootstrap
from .evidence import EvidenceReader, InitialCopyEvidence, copy_intent_key, evidence_key

BOUNDARY_FORMAT = "quadringent-local-boundary-v1"
RESULT_PREFIX = "quadringent_local_result="
ERROR_PREFIX = "quadringent_local_error="

ENV_IBMI_PASSWORD_FILE = PASSWORD_FILE_ENV = "QUADRINGENT_LOCAL_IBMI_PASSWORD_FILE"
ENV_SNOWFLAKE_KEY_FILE = "QUADRINGENT_LOCAL_SNOWFLAKE_KEY_FILE"
ENV_CAPTURE_SCRIPT = "QUADRINGENT_LOCAL_CAPTURE_SCRIPT"
ENV_LOADER_SCRIPT = "QUADRINGENT_LOCAL_LOADER_SCRIPT"
DEFAULT_IBMI_PASSWORD_FILE = "/run/secrets/ibmi_password"
DEFAULT_SNOWFLAKE_KEY_FILE = "/run/secrets/snowflake_key.p8"
DEFAULT_CAPTURE_SCRIPT = "/app/as400_continuous_capture.py"
DEFAULT_LOADER_SCRIPT = "/app/quadringent_destination_loader.py"
DEFAULT_SCRIPTS_DIR = "/app"

BOUNDARY_TIMEOUT_SECONDS = 30.0
RECEIVER_METADATA_LIMIT = 8
MAX_SECRET_BYTES = 65536
MAX_ERROR_CHARS = 300
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RAW_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}")

# Values read from secret files during this process; scrubbed from any error text.
_SECRET_VALUES: list[str] = []


class LocalRuntimeError(RuntimeError):
    """A refused or failed step. The message is safe to print: it never carries a secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- secrets -------------------------------------------------------------------------------------


def read_secret_file(path: str, *, strip_final_newline: bool = False) -> str:
    """Read one mounted secret file: regular, not a symlink, non-empty, bounded."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise LocalRuntimeError("secret_unavailable", "secret file is missing or unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_BYTES:
            raise LocalRuntimeError("secret_invalid", "secret file must be a small regular file")
        content = os.read(descriptor, MAX_SECRET_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LocalRuntimeError("secret_invalid", "secret file is not UTF-8 text") from error
    if strip_final_newline:
        for ending in ("\r\n", "\n"):
            if value.endswith(ending):
                value = value[: -len(ending)]
                break
    if not value.strip():
        raise LocalRuntimeError("secret_invalid", "secret file is empty")
    for candidate in {value, value.strip()}:
        if candidate and candidate not in _SECRET_VALUES:
            _SECRET_VALUES.append(candidate)
    return value


def _ibmi_password(path: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    chosen = path or environ.get(ENV_IBMI_PASSWORD_FILE) or DEFAULT_IBMI_PASSWORD_FILE
    return read_secret_file(chosen, strip_final_newline=True)


def _snowflake_key(path: str | None = None) -> str:
    chosen = path or os.environ.get(ENV_SNOWFLAKE_KEY_FILE) or DEFAULT_SNOWFLAKE_KEY_FILE
    return read_secret_file(chosen)


def redact(text: str, secrets: Sequence[str] | None = None) -> str:
    """One bounded line with every known secret value removed."""
    clean = " ".join(str(text).split())
    for secret in sorted(set(_SECRET_VALUES if secrets is None else secrets), key=len, reverse=True):
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return clean[:MAX_ERROR_CHARS]


# -- shared helpers ------------------------------------------------------------------------------


def _env(name: str, environ: Mapping[str, str] | None = None) -> str:
    value = ((environ if environ is not None else os.environ).get(name) or "").strip()
    if not value:
        raise LocalRuntimeError("config_missing", f"{name} is required")
    return value


def _local_storage(environ: Mapping[str, str] | None = None) -> StorageBackend:
    environ = os.environ if environ is None else environ
    if (environ.get("QUADRINGENT_STORAGE_BACKEND") or "").strip().lower() != "local":
        raise LocalRuntimeError("config_invalid", "QUADRINGENT_STORAGE_BACKEND must be local")
    try:
        return StorageBackend.from_environment(environ)
    except ValueError as error:
        raise LocalRuntimeError("storage_invalid", str(error)) from error


def _identity_argument(name: str, value: str) -> str:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise LocalRuntimeError("config_invalid", f"{name} is not a canonical identifier")
    return value


def _raw_prefix_root(value: str) -> str:
    clean = (value or "").strip().strip("/")
    if _RAW_PREFIX.fullmatch(clean) is None or any(part in ("", ".", "..") for part in clean.split("/")):
        raise LocalRuntimeError("config_invalid", "raw prefix root is not a canonical relative path")
    return clean


def emit(result: Mapping[str, Any], stream=None) -> None:
    (stream or sys.stdout).write(RESULT_PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    (stream or sys.stdout).flush()


# -- fs-probe ------------------------------------------------------------------------------------


def command_fs_probe(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    data_dir = args.data_dir or _env(LOCAL_DATA_DIR_ENV)
    try:
        result = probe_local_filesystem(data_dir)
    except ValueError as error:
        raise LocalRuntimeError("config_invalid", str(error)) from error
    return result, 0 if result["ok"] else 1


# -- init-data and read-status (data kept in a Docker named volume) --------------------------------


MARKER_FORMAT_VERSION = 1
MAX_CONSOLE_BYTES = 1_048_576
MAX_EVIDENCE_READ_BYTES = 1_048_576
DATA_SUBDIRECTORIES = ("console", "scratch")


def _data_directory(args: argparse.Namespace) -> str:
    path = args.data_dir or _env(LOCAL_DATA_DIR_ENV)
    try:
        info = os.lstat(path)
    except OSError as error:
        raise LocalRuntimeError("config_invalid", "data directory does not exist") from error
    if not stat.S_ISDIR(info.st_mode):
        raise LocalRuntimeError("config_invalid", "data directory must be a real directory, not a link")
    return path


def _ensure_directory(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise LocalRuntimeError("config_invalid", "a data sub-directory is not a real directory")


def _write_marker(data_dir: str, label: str) -> bool:
    path = os.path.join(data_dir, LOCAL_MARKER_NAME)
    document = (json.dumps({"format_version": MARKER_FORMAT_VERSION, "label": label}, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, document)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def command_init_data(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Prepare ``/data`` of a fresh Docker volume (root-owned) for the containers that run as the host user.

    Creates the marker, ``console/`` and ``scratch/`` (0700), verifies an existing marker for this exact label,
    then hands the top level over to ``--uid:--gid``. Only the top level is touched: files that earlier runs
    wrote keep the owner they have.
    """
    data_dir = _data_directory(args)
    try:
        label = validate_local_label(args.label or _env("AS400_RAW_BUCKET"))
    except ValueError as error:
        raise LocalRuntimeError("config_invalid", str(error)) from error
    if args.uid < 0 or args.gid < 0:
        raise LocalRuntimeError("config_invalid", "uid and gid must not be negative")
    entries = [os.path.join(data_dir, name) for name in DATA_SUBDIRECTORIES]
    try:
        for entry in entries:
            _ensure_directory(entry)
        created = _write_marker(data_dir, label)
        try:
            require_local_marker(data_dir, label)
        except ValueError as error:
            raise LocalRuntimeError("storage_invalid", str(error)) from error
        owned = os.geteuid() == os.lstat(data_dir).st_uid
        if owned and stat.S_IMODE(os.lstat(data_dir).st_mode) != 0o700:
            os.chmod(data_dir, 0o700)
        for entry in (data_dir, *entries, os.path.join(data_dir, LOCAL_MARKER_NAME)):
            info = os.lstat(entry)
            if (info.st_uid, info.st_gid) != (args.uid, args.gid):
                os.lchown(entry, args.uid, args.gid)
    except OSError as error:
        raise LocalRuntimeError("storage_invalid", f"cannot prepare the data directory: {type(error).__name__}") from error
    return {"ok": True, "created": created, "label": label}, 0


def _read_bounded_json(path: str, limit: int) -> Any | None:
    """A small regular JSON file, or None when it is absent, a link, too large or not JSON."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        content = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(content) > limit:
        return None
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def command_read_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Read-only view for ``status``: the console snapshot, and ``rows_copied`` of the copy evidence if asked."""
    data_dir = _data_directory(args)
    console = _read_bounded_json(os.path.join(data_dir, "console", "capture.json"), MAX_CONSOLE_BYTES)
    rows: int | None = None
    if args.evidence_key:
        key = _raw_prefix_root(args.evidence_key)
        try:
            label = validate_local_label(_env("AS400_RAW_BUCKET"))
        except ValueError as error:
            raise LocalRuntimeError("config_invalid", str(error)) from error
        evidence = _read_bounded_json(os.path.join(data_dir, "objects", label, *key.split("/")), MAX_EVIDENCE_READ_BYTES)
        value = evidence.get("rows_copied") if isinstance(evidence, dict) else None
        rows = value if type(value) is int and value >= 0 else None
    return {"console": console if isinstance(console, dict) else None, "evidence_rows": rows}, 0


# -- boundary ------------------------------------------------------------------------------------


def _attached_receiver(snapshots):
    attached = [item for item in snapshots if item.status == "ATTACHED"]
    return attached[0] if len(attached) == 1 else None


def read_boundary_from_catalog(catalog, *, journal_library: str, journal_name: str,
                               now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> JournalBoundary:
    """Same acquisition sequence as ``JavaBoundaryReader.read_boundary`` over any receiver catalog.

    Mirrored because that class needs SQLAlchemy and the credential store of the
    cluster control plane; ``tests/test_local_runtime_admission.py`` holds the parity.
    """
    try:
        snapshots = catalog.snapshot()
        attached = _attached_receiver(snapshots)
        if attached is None:
            raise LocalRuntimeError("capability_unavailable", "no unique ATTACHED receiver for this journal")
        identity_args = dict(journal_library=journal_library, journal_name=journal_name,
                             receiver_library=attached.receiver_library, receiver_name=attached.receiver,
                             asp_number=1)
        # System ASP 1 is the supported policy; the native provider checks it.
        before = catalog.identity(**identity_args)
        fresh = _attached_receiver(catalog.snapshot(required_receiver=attached.receiver))
        if fresh is None or (fresh.receiver_library, fresh.receiver) != (attached.receiver_library, attached.receiver):
            raise LocalRuntimeError("executor_unavailable", "receiver changed during boundary acquisition")
        identity = catalog.identity(**identity_args)
        if type(identity) is not ReceiverIdentity or identity != before:
            raise LocalRuntimeError("executor_unavailable", "native receiver identity changed during acquisition")
        if (identity.journal_library, identity.journal_name, identity.receiver_library, identity.receiver_name) != (
            journal_library, journal_name, fresh.receiver_library, fresh.receiver
        ):
            raise LocalRuntimeError("executor_unavailable", "native identity differs from receiver metadata")
        attached = fresh
    except LocalRuntimeError:
        raise
    except Exception as error:  # noqa: BLE001 - never forward remote diagnostics
        raise LocalRuntimeError("executor_unavailable", "cannot read the journal position") from error
    last_sequence = attached.last_sequence
    if (attached.first_sequence is not None and
            (type(attached.first_sequence) is not int or attached.first_sequence < 1)):
        raise LocalRuntimeError("executor_unavailable", "invalid receiver first sequence")
    if last_sequence is not None and (
        type(last_sequence) is not int or last_sequence < 0
        or (attached.first_sequence is not None and last_sequence < attached.first_sequence - 1)
    ):
        raise LocalRuntimeError("executor_unavailable", "invalid receiver sequence bounds")
    if last_sequence is None:
        if attached.first_sequence is None:
            raise LocalRuntimeError("capability_unavailable", "the ATTACHED receiver has no usable sequence yet")
        last_sequence = attached.first_sequence - 1
    try:
        return JournalBoundary(
            receiver_library=attached.receiver_library,
            receiver_name=attached.receiver,
            last_sequence=last_sequence,
            observed_at=now(),
            receiver_identity=identity,
        )
    except BoundaryError as error:
        raise LocalRuntimeError("executor_unavailable", "Invalid journal position") from error


def observed_boundary_to_dict(boundary: JournalBoundary) -> dict[str, Any]:
    """Serialise a native boundary that is not yet bound (``JournalBoundary.to_dict`` refuses it)."""
    if boundary.receiver_identity is None or boundary.capture_binding is not None:
        raise LocalRuntimeError("config_invalid", "boundary must be native and not yet bound")
    return {
        "format_version": BOUNDARY_FORMAT,
        "receiver_library": boundary.receiver_library,
        "receiver_name": boundary.receiver_name,
        "last_sequence": boundary.last_sequence,
        "observed_at": boundary.observed_at.astimezone(timezone.utc).isoformat(),
        "receiver_identity": boundary.receiver_identity.to_dict(),
    }


def observed_boundary_from_dict(payload: object) -> JournalBoundary:
    fields = {"format_version", "receiver_library", "receiver_name", "last_sequence", "observed_at",
              "receiver_identity"}
    if type(payload) is not dict or set(payload) != fields or payload["format_version"] != BOUNDARY_FORMAT:
        raise LocalRuntimeError("config_invalid", "boundary document has unexpected fields")
    try:
        identity = ReceiverIdentity.from_dict(payload["receiver_identity"])
        if type(payload["last_sequence"]) is not int:
            raise ValueError("sequence")
        return JournalBoundary(
            receiver_library=payload["receiver_library"], receiver_name=payload["receiver_name"],
            last_sequence=payload["last_sequence"], observed_at=datetime.fromisoformat(payload["observed_at"]),
            receiver_identity=identity,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise LocalRuntimeError("config_invalid", "boundary document is malformed") from error


def read_boundary(environ: Mapping[str, str], *, catalog_factory=None,
                  now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  timeout_seconds: float = BOUNDARY_TIMEOUT_SECONDS) -> JournalBoundary:
    """Read the attached receiver of the journal named by ``environ`` (password from its secret file).

    ``catalog_factory`` takes the keyword arguments of ``boundary_reader.ReceiverCatalogFactory``;
    the default builds a ``JavaReceiverCatalog`` exactly as ``JavaBoundaryReader`` does.
    """
    journal_library = _env("AS400_JOURNAL_LIBRARY", environ)
    journal_name = _env("AS400_JOURNAL_NAME", environ)
    host, user = _env("ISERIES_HOST", environ), _env("ISERIES_USER", environ)
    classpath = _env("AS400_JAVA_CLASSPATH", environ)
    password = _ibmi_password(None, environ)
    if catalog_factory is None:
        def catalog_factory(*, host, user, password, journal_library, journal_name, ca_file=None):
            from quadringent import java_catalog

            return java_catalog.JavaReceiverCatalog(
                java=environ.get("AS400_JAVA") or "java", classpath=classpath, host=host, user=user,
                journal_library=journal_library, journal_name=journal_name, limit=RECEIVER_METADATA_LIMIT,
                timeout_seconds=timeout_seconds, password=password, ca_file=ca_file)
    catalog = catalog_factory(host=host, user=user, password=password, journal_library=journal_library,
                              journal_name=journal_name, ca_file=environ.get("AS400_TLS_CA_FILE") or None)
    return read_boundary_from_catalog(catalog, journal_library=journal_library, journal_name=journal_name, now=now)


def command_boundary(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    environ = dict(os.environ)
    if args.password_file:
        environ[PASSWORD_FILE_ENV] = args.password_file
    boundary = read_boundary(environ, timeout_seconds=args.timeout_seconds)
    return observed_boundary_to_dict(boundary), 0


# -- admit ---------------------------------------------------------------------------------------


def _registry(storage: StorageBackend, *, source_id: str, journal_library: str, journal_name: str,
              raw_prefix_root: str) -> tuple[ReceiverNameRegistry, str]:
    scope_id = reader_checkpoint_stream_key(
        storage_backend=storage.kind, source_id=source_id, journal_library=journal_library,
        journal_name=journal_name, raw_bucket=storage.raw_bucket, raw_prefix=raw_prefix_root,
    )
    registry = ReceiverNameRegistry(storage.object_store("receiver-authority-v1"),
                                    authority=registry_authority(storage.kind, storage.raw_bucket),
                                    source_system="ibmi", journal=journal_name)
    return registry, scope_id


def admit_boundary(storage: StorageBackend, boundary: JournalBoundary, *, source_id: str, journal_library: str,
                   journal_name: str, raw_prefix_root: str) -> tuple[JournalBoundary, CaptureBinding]:
    """Original native admission of the capture scope.

    Reproduces ``KubernetesPipelineExecutor._start`` (legacy checkpoint refusal,
    ``plan_bootstrap``) followed by ``_admit_boundary`` on the local backend.
    """
    if boundary.receiver_identity is None:
        raise LocalRuntimeError("capability_unavailable", "native receiver identity is required before initial copy")
    if (boundary.receiver_identity.journal_library, boundary.receiver_identity.journal_name) != (
        journal_library, journal_name
    ):
        raise LocalRuntimeError("capability_unavailable", "native journal differs from the declared copy scope")
    registry, scope_id = _registry(storage, source_id=source_id, journal_library=journal_library,
                                   journal_name=journal_name, raw_prefix_root=raw_prefix_root)
    try:
        storage.checkpoint_store(scope_id).load_bound()
    except ValueError as error:
        raise LocalRuntimeError("capability_unavailable", "legacy checkpoint cannot be adopted by initial copy") from error
    boundary = plan_bootstrap(boundary)
    try:
        checkpoint = storage.checkpoint_store(scope_id).load_bound()
        binding = registry.load_binding(scope_id)
        if binding is None:
            if checkpoint is not None:
                raise ValueError("checkpoint exists without its original capture binding")
            if storage.object_store(raw_prefix_root).list_receipt_keys(1):
                raise ValueError("existing RAW requires explicit capture migration")
        binding = registry.create_or_load_binding(scope_id=scope_id, identity=boundary.receiver_identity,
                                                  sequence=boundary.last_sequence)
        if checkpoint is not None:
            registry.require(binding, checkpoint)
        bound = boundary.with_binding(binding)
        registry.claim(binding, bound.require_bound())
        registry.require(binding, bound.require_bound())
        return bound, binding
    except ValueError as error:
        raise LocalRuntimeError("capability_unavailable",
                                "capture admission requires an original native binding") from error


def write_copy_intent(storage: StorageBackend, *, pipeline_id: str, table_id: str, run_id: str,
                      boundary: JournalBoundary) -> None:
    intent = {"format_version": "quadringent-copy-intent-v1", "pipeline_id": pipeline_id,
              "table_id": table_id, "run_id": run_id, "boundary": boundary.to_dict()}
    storage.object_store("receiver-authority-v1").put_once(
        copy_intent_key(pipeline_id, table_id, run_id),
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())


def run_admission(storage: StorageBackend, boundary: JournalBoundary, *, source_id: str, pipeline_id: str,
                  table_id: str, journal_library: str, journal_name: str, raw_prefix_root: str,
                  run_id: str | None = None) -> dict[str, Any]:
    raw_prefix_root = _raw_prefix_root(raw_prefix_root)
    for name, value in (("source id", source_id), ("pipeline id", pipeline_id), ("table id", table_id)):
        _identity_argument(name, value)
    run_id = run_id or str(uuid4())
    if _RUN_ID.fullmatch(run_id) is None:
        raise LocalRuntimeError("config_invalid", "run id must be a UUID")
    bound, binding = admit_boundary(storage, boundary, source_id=source_id, journal_library=journal_library,
                                    journal_name=journal_name, raw_prefix_root=raw_prefix_root)
    try:
        write_copy_intent(storage, pipeline_id=pipeline_id, table_id=table_id, run_id=run_id, boundary=bound)
    except ValueError as error:
        raise LocalRuntimeError("capability_unavailable", "a different copy intent is already recorded for this run") from error
    return {
        "binding": binding.to_dict(),
        "boundary": bound.to_dict(),
        "run_id": run_id,
        "evidence_key": evidence_key(raw_prefix_root, table_id, run_id),
        "raw_prefix_root": raw_prefix_root,
        "stream_key": _registry(storage, source_id=source_id, journal_library=journal_library,
                                journal_name=journal_name, raw_prefix_root=raw_prefix_root)[1],
        "table_id": table_id,
        "pipeline_id": pipeline_id,
    }


def _json_argument(inline: str | None, path: str | None, name: str) -> object:
    if (inline is None) == (path is None):
        raise LocalRuntimeError("config_invalid", f"exactly one of --{name}-json and --{name}-file is required")
    try:
        if path is not None:
            raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        else:
            raw = inline
        return _decode_checkpoint_json(raw)
    except (OSError, ValueError) as error:
        raise LocalRuntimeError("config_invalid", f"{name} document is unreadable") from error


REQUEST_FIELDS = {"source_id", "pipeline_id", "table_id", "journal_library", "journal_name", "raw_prefix",
                  "boundary"}
MAX_REQUEST_BYTES = 65536


def _parse_request(text: str) -> dict[str, Any]:
    """Strict admission request: exactly the declared fields (plus an optional ``run_id``)."""
    if type(text) is not str or len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise LocalRuntimeError("config_invalid", "admission request is missing or too large")
    try:
        request = _decode_checkpoint_json(text)
    except ValueError as error:
        raise LocalRuntimeError("config_invalid", "admission request is not valid JSON") from error
    if type(request) is not dict or not REQUEST_FIELDS <= set(request) <= REQUEST_FIELDS | {"run_id"}:
        raise LocalRuntimeError("config_invalid", "admission request has unexpected fields")
    for name in sorted(set(request) - {"boundary"}):
        value = request[name]
        if type(value) is not str or not value or value.strip() != value:
            raise LocalRuntimeError("config_invalid", f"admission request field {name} must be a plain string")
    if type(request["boundary"]) is not dict:
        raise LocalRuntimeError("config_invalid", "admission request boundary must be an object")
    if "run_id" in request and _RUN_ID.fullmatch(request["run_id"]) is None:
        raise LocalRuntimeError("config_invalid", "run id must be a UUID")
    return request


def admit(request: Mapping[str, Any], storage: StorageBackend) -> dict[str, Any]:
    """Original admission and copy intent for one request (see ``run_admission``)."""
    if storage.kind != "local":
        raise LocalRuntimeError("config_invalid", "admission runs on the local storage backend only")
    request = _parse_request(json.dumps(request))
    return run_admission(
        storage, observed_boundary_from_dict(request["boundary"]), source_id=request["source_id"],
        pipeline_id=request["pipeline_id"], table_id=request["table_id"],
        journal_library=request["journal_library"], journal_name=request["journal_name"],
        raw_prefix_root=request["raw_prefix"], run_id=request.get("run_id"))


def command_admit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Admission from a request document, or from the boundary result and identity flags."""
    storage = _local_storage()
    if args.request_json is not None or args.request_file is not None:
        if (args.request_json is None) == (args.request_file is None) or args.boundary_json or args.boundary_file:
            raise LocalRuntimeError("config_invalid", "give either one request document or the boundary and identity flags")
        try:
            text = args.request_json if args.request_file is None else (
                sys.stdin.read() if args.request_file == "-" else Path(args.request_file).read_text(encoding="utf-8"))
        except OSError as error:
            raise LocalRuntimeError("config_invalid", "admission request is unreadable") from error
        return admit(_parse_request(text), storage), 0
    boundary = _json_argument(args.boundary_json, args.boundary_file, "boundary")
    request = {
        "source_id": args.source_id, "pipeline_id": args.pipeline_id, "table_id": args.table_id,
        "journal_library": _env("AS400_JOURNAL_LIBRARY").upper(), "journal_name": _env("AS400_JOURNAL_NAME").upper(),
        "raw_prefix": args.raw_prefix_root, "boundary": boundary,
    }
    if args.run_id:
        request["run_id"] = args.run_id
    for name in ("source_id", "pipeline_id", "table_id", "raw_prefix"):
        if not request[name]:
            raise LocalRuntimeError("config_invalid", f"--{name.replace('_', '-')}{'-root' if name == 'raw_prefix' else ''} is required")
    return admit(_parse_request(json.dumps(request)), storage), 0


# -- copy ----------------------------------------------------------------------------------------


def admission_from_dict(payload: object) -> tuple[dict[str, Any], JournalBoundary, CaptureBinding]:
    fields = {"binding", "boundary", "run_id", "evidence_key", "raw_prefix_root", "stream_key", "table_id",
              "pipeline_id"}
    if type(payload) is not dict or set(payload) != fields:
        raise LocalRuntimeError("config_invalid", "admission document has unexpected fields")
    try:
        binding = CaptureBinding.from_dict(payload["binding"])
        boundary = JournalBoundary.from_dict(payload["boundary"])
        boundary.require_bound()
    except (KeyError, TypeError, ValueError) as error:
        raise LocalRuntimeError("config_invalid", "admission document is malformed") from error
    if boundary.capture_binding != binding:
        raise LocalRuntimeError("config_invalid", "admission boundary differs from its capture binding")
    if _RUN_ID.fullmatch(str(payload["run_id"])) is None:
        raise LocalRuntimeError("config_invalid", "run id must be a UUID")
    _raw_prefix_root(payload["raw_prefix_root"])
    for name in ("table_id", "pipeline_id"):
        _identity_argument(name, payload[name])
    if payload["evidence_key"] != evidence_key(payload["raw_prefix_root"], payload["table_id"], payload["run_id"]):
        raise LocalRuntimeError("config_invalid", "evidence key differs from its run")
    return payload, boundary, binding


def copy_job_environment(base: Mapping[str, str], admission: Mapping[str, Any], boundary: JournalBoundary,
                          binding: CaptureBinding, *, snapshot_dir: str, schema: str, table: str,
                          copy_observation_request: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The environment ``build_initial_copy_job`` would give the copy Job, on the local backend."""
    env = dict(base)
    if copy_observation_request is not None:
        from quadringent.copy_observation import validate_request

        request = validate_request(dict(copy_observation_request))
        if (request['source']['library'], request['source']['file']) != (schema, table):
            raise LocalRuntimeError('config_invalid', 'copy observation source differs from the requested table')
        env['AS400_COPY_OBSERVATION_REQUEST_JSON'] = json.dumps(request, sort_keys=True, separators=(',', ':'))
    env.update({
        "QUADRINGENT_STORAGE_BACKEND": "local",
        "AS400_RAW_PREFIX": admission["raw_prefix_root"],
        "AS400_SNAPSHOT_RUN_ID": admission["run_id"],
        "AS400_SNAPSHOT_OUTPUT_DIR": snapshot_dir,
        "ISERIES_SCHEMA": schema,
        "ISERIES_TABLE": table,
        "AS400_BOOTSTRAP_RECEIVER": boundary.receiver_name,
        "AS400_BOOTSTRAP_RECEIVER_LIBRARY": boundary.receiver_library,
        "AS400_BOOTSTRAP_SEQUENCE": str(boundary.bootstrap_sequence),
        "AS400_BOOTSTRAP_OBSERVED_AT": boundary.observed_at.isoformat(),
        "AS400_EVIDENCE_KEY": admission["evidence_key"],
        "AS400_PIPELINE_ID": admission["pipeline_id"],
        "AS400_TABLE_ID": admission["table_id"],
        "AS400_CAPTURE_BINDING_JSON": json.dumps(binding.to_dict(), sort_keys=True, separators=(",", ":")),
        "AS400_BOUNDARY_JSON": json.dumps(boundary.to_dict(), sort_keys=True, separators=(",", ":")),
    })
    return env


def _copy_job_module():
    try:
        import quadringent_initial_copy_job
    except ImportError:
        sys.path.insert(0, os.environ.get("QUADRINGENT_LOCAL_SCRIPTS_DIR") or DEFAULT_SCRIPTS_DIR)
        import quadringent_initial_copy_job
    return quadringent_initial_copy_job


def run_copy(storage: StorageBackend, admission: Mapping[str, Any], boundary: JournalBoundary,
             binding: CaptureBinding, *, schema: str, table: str, password: str | None,
             environ: Mapping[str, str] | None = None, copy_job=None, now=None, catalog_factory=None,
             copy_observation_request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run the initial copy job on local storage; a run whose evidence is already durable is not repeated."""
    reader = EvidenceReader(storage.object_store(""))
    existing = reader.read(admission["evidence_key"], expected_source=(schema, table))
    if existing is not None:
        if (existing.pipeline_id, existing.table_id, existing.run_id) != (
                admission["pipeline_id"], admission["table_id"], admission["run_id"]) or existing.boundary != boundary:
            raise LocalRuntimeError("capability_unavailable", "existing copy evidence belongs to another admission")
        requested = copy_observation_request is not None or 'AS400_COPY_OBSERVATION_REQUEST_JSON' in (environ or os.environ)
        if requested and existing.copy_observations is None:
            raise LocalRuntimeError('capability_unavailable', 'historical copy cannot acquire observations retroactively')
        return {"evidence_key": admission["evidence_key"], "rows": existing.rows_copied}
    module = copy_job or _copy_job_module()
    scratch = Path(storage.state_location) / "scratch" / admission["run_id"]
    # A previous attempt of this exact run may have left a partial snapshot; the reader needs an empty directory.
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ if environ is None else environ)
    if password is not None:
        base["ISERIES_PASSWORD"] = password
    env = copy_job_environment(base, admission, boundary, binding, snapshot_dir=str(scratch),
                               schema=schema, table=table, copy_observation_request=copy_observation_request)
    env.setdefault(LOCAL_DATA_DIR_ENV, storage.state_location)
    os.environ.setdefault(LOCAL_DATA_DIR_ENV, storage.state_location)
    try:
        # The job logs one progress line on stdout; keep stdout for the single result line.
        with contextlib.redirect_stdout(sys.stderr):
            code = module.main([], environ=env, now=now, catalog_factory=catalog_factory)
    except module.InitialCopyJobError as error:
        raise LocalRuntimeError("copy_failed", redact(str(error))) from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if code != 0:
        raise LocalRuntimeError("copy_failed", "initial copy job returned a failure status")
    evidence = reader.read(admission["evidence_key"], expected_source=(schema, table))
    if evidence is None:
        raise LocalRuntimeError("copy_failed", "initial copy finished without durable evidence")
    return {"evidence_key": admission["evidence_key"], "rows": evidence.rows_copied}


def command_copy(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    storage = _local_storage()
    admission, boundary, binding = admission_from_dict(_json_argument(None, args.admission_file, "admission"))
    password = _ibmi_password(args.password_file)
    result = run_copy(storage, admission, boundary, binding,
                      schema=args.schema or _env("ISERIES_SCHEMA"), table=args.table or _env("ISERIES_TABLE"),
                      password=password)
    return result, 0


# -- snowflake-verify ----------------------------------------------------------------------------


def command_snowflake_verify(args: argparse.Namespace, *, verifier=None) -> tuple[dict[str, Any], int]:
    from quadringent_control_plane.v2.services.destination_verifier import (
        DestinationVerifierError,
        DestinationVerifierRequest,
        SnowflakeKeyPairVerifier,
        warehouse_name_for,
    )

    role = _env("SNOWFLAKE_ROLE")
    try:
        warehouse = warehouse_name_for(role)
    except DestinationVerifierError as error:
        raise LocalRuntimeError("config_invalid", str(error)) from error
    request = DestinationVerifierRequest(
        snowflake_account=_env("SNOWFLAKE_ACCOUNT"), service_user=_env("SNOWFLAKE_USER"), service_role=role,
        warehouse=warehouse, private_key_pem=_snowflake_key(args.key_file),
        destination_database=_env("QUADRINGENT_DESTINATION_DATABASE"),
        destination_schema=_env("QUADRINGENT_DESTINATION_SCHEMA"),
    )
    outcome = (verifier or SnowflakeKeyPairVerifier()).verify(request).to_dict()
    checks = {name: value for name, value in outcome.items() if name != "verified"}
    return {"ok": bool(outcome["verified"]), "checks": checks}, 0 if outcome["verified"] else 1


# -- exec wrappers -------------------------------------------------------------------------------


def exec_with_secret(script: str, argv: Sequence[str], *, secret_name: str, secret_value: str,
                     python_flags: Sequence[str] = (), execve=os.execve) -> None:
    """Replace this process by ``python script argv`` with one secret in its environment only."""
    if not os.path.isabs(script):
        raise LocalRuntimeError("config_invalid", "script path must be absolute")
    environment = dict(os.environ)
    environment[secret_name] = secret_value
    os.umask(0o077)  # data written by the child stays private to the container user
    executable = sys.executable or "python"
    execve(executable, [executable, *python_flags, script, *argv], environment)


def _child_arguments(values: Sequence[str]) -> list[str]:
    arguments = list(values)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return arguments


def command_exec_capture(args: argparse.Namespace, *, execve=os.execve) -> tuple[dict[str, Any], int]:
    password = _ibmi_password(args.password_file)
    script = args.script or os.environ.get(ENV_CAPTURE_SCRIPT) or DEFAULT_CAPTURE_SCRIPT
    exec_with_secret(script, _child_arguments(args.child_arguments), secret_name="ISERIES_PASSWORD",
                     secret_value=password, execve=execve)
    return {}, 0


def command_exec_loader(args: argparse.Namespace, *, execve=os.execve) -> tuple[dict[str, Any], int]:
    key = _snowflake_key(args.key_file)
    script = args.script or os.environ.get(ENV_LOADER_SCRIPT) or DEFAULT_LOADER_SCRIPT
    # -P mirrors the cluster command: the script directory never shadows installed modules.
    exec_with_secret(script, _child_arguments(args.child_arguments), secret_name="SNOWFLAKE_PRIVATE_KEY_PEM",
                     secret_value=key, python_flags=("-P",), execve=execve)
    return {}, 0


# -- entry point ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local_runtime", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("fs-probe", help="check the data directory filesystem")
    probe.add_argument("--data-dir", default=None)

    init_data = commands.add_parser("init-data", help="prepare a fresh data directory (Docker volume)")
    init_data.add_argument("--data-dir", default=None)
    init_data.add_argument("--label", default=None)
    init_data.add_argument("--uid", type=int, required=True)
    init_data.add_argument("--gid", type=int, required=True)

    read_status = commands.add_parser("read-status", help="read the console snapshot for status")
    read_status.add_argument("--data-dir", default=None)
    read_status.add_argument("--evidence-key", default=None)

    boundary = commands.add_parser("boundary", help="read the attached journal receiver boundary")
    boundary.add_argument("--password-file", default=None)
    boundary.add_argument("--timeout-seconds", type=float, default=BOUNDARY_TIMEOUT_SECONDS)

    admit = commands.add_parser("admit", help="original capture admission and copy intent")
    admit.add_argument("--request-json", default=None, help="complete admission request document")
    admit.add_argument("--request-file", default=None, help="admission request document, or - for stdin")
    admit.add_argument("--boundary-json", default=None, help="result of the boundary command")
    admit.add_argument("--boundary-file", default=None, help="boundary result document, or - for stdin")
    admit.add_argument("--source-id", default=None)
    admit.add_argument("--pipeline-id", default=None)
    admit.add_argument("--table-id", default=None)
    admit.add_argument("--raw-prefix-root", default=None)
    admit.add_argument("--run-id", default=None)

    copy = commands.add_parser("copy", help="run the initial copy for an admitted run")
    copy.add_argument("--admission-file", required=True, help="admission document, or - for stdin")
    copy.add_argument("--schema", default=None)
    copy.add_argument("--table", default=None)
    copy.add_argument("--password-file", default=None)

    verify = commands.add_parser("snowflake-verify", help="verify the Snowflake destination")
    verify.add_argument("--key-file", default=None)

    for name, helptext in (("exec-capture", "run the capture reader"), ("exec-loader", "run the destination loader")):
        wrapper = commands.add_parser(name, help=helptext)
        wrapper.add_argument("--script", default=None)
        wrapper.add_argument("--password-file" if name == "exec-capture" else "--key-file", default=None)
        wrapper.add_argument("child_arguments", nargs=argparse.REMAINDER)
    return parser


COMMANDS: dict[str, Callable[..., tuple[dict[str, Any], int]]] = {
    "init-data": command_init_data,
    "read-status": command_read_status,
    "fs-probe": command_fs_probe,
    "boundary": command_boundary,
    "admit": command_admit,
    "copy": command_copy,
    "snowflake-verify": command_snowflake_verify,
    "exec-capture": command_exec_capture,
    "exec-loader": command_exec_loader,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(0o077)
    try:
        result, code = COMMANDS[args.command](args)
    except LocalRuntimeError as error:
        print(ERROR_PREFIX + redact(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - only the type name may leave the process
        print(ERROR_PREFIX + redact(f"unexpected failure: {type(error).__name__}"), file=sys.stderr)
        return 1
    if result:
        emit(result)
    if code != 0:
        print(ERROR_PREFIX + redact(f"{args.command} reported a failed check"), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
