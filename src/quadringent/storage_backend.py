"""Selection of the durable capture backend from the process environment.

``QUADRINGENT_STORAGE_BACKEND`` chooses where raw batches, checkpoints and
the source gate live:

* ``aws`` (default): S3 bucket ``AS400_RAW_BUCKET`` and DynamoDB table
  ``AS400_CHECKPOINT_TABLE`` — unchanged behaviour;
* ``gcs``: GCS bucket ``AS400_RAW_BUCKET`` for raw batches and GCS bucket
  ``AS400_CHECKPOINT_BUCKET`` for checkpoint and source-gate records;
* ``local``: one directory on a local POSIX filesystem
  (``AS400_LOCAL_DATA_DIR``) holding a store labelled ``AS400_RAW_BUCKET``.
  Single machine only: every process that shares the store must see the same
  kernel (hard links, ``flock`` and atomic rename are the only primitives).

The ordering guarantees (raw durable before checkpoint, compare-and-set,
explicit receiver transitions) are identical for all backends.

Local layout below ``<data>``::

    .quadringent-local                          marker {"format_version": 1, "label": ...}
    objects/<label>/<prefix...>                 FileObjectStore root
    state/<label>/checkpoints/<slug>-<sha>.json JsonCheckpointStore (+ .lock)
    state/<label>/source-gates/<sha>.json       FileSourceGate (+ .lock)

Only the standard library is imported at module load, so host tooling can
import the helpers on any platform; the POSIX-only pieces are imported lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import secrets
import stat
from typing import Any, Callable, Mapping

BACKENDS = ("aws", "gcs", "local")

LOCAL_MARKER_NAME = ".quadringent-local"
LOCAL_FORMAT_VERSION = 1
LOCAL_DATA_DIR_ENV = "AS400_LOCAL_DATA_DIR"
LOCAL_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
_MARKER_MAX_BYTES = 4096
_AUTHORITY_SCHEMES = {"aws": "s3", "s3": "s3", "gcs": "gcs", "local": "local"}

# Filesystems whose hard link, flock or rename semantics are not those of one
# local kernel (network and FUSE mounts, WSL2 Windows drives) or that do not
# survive a restart. The behavioural probe still has the last word.
UNSUPPORTED_FILESYSTEMS = frozenset({
    "nfs", "nfs3", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "9p", "drvfs", "afs", "ceph",
    "glusterfs", "lustre", "tmpfs", "ramfs", "vfat", "msdos", "exfat", "ntfs", "ntfs3",
})


def backend_from_environment(environ: Mapping[str, str]) -> str:
    backend = (environ.get("QUADRINGENT_STORAGE_BACKEND") or "aws").strip().lower()
    if backend not in BACKENDS:
        raise ValueError("QUADRINGENT_STORAGE_BACKEND must be aws, gcs or local")
    return backend


def registry_authority(kind: str, bucket: str) -> str:
    """Stable receiver-registry namespace of one store (``s3://``, ``gcs://`` or ``local://``)."""
    scheme = _AUTHORITY_SCHEMES.get(kind)
    if scheme is None:
        raise ValueError("unknown storage backend for the receiver registry authority")
    return f"{scheme}://{bucket}/receiver-authority-v1"


def _required(environ: Mapping[str, str], name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def validate_local_label(label: object) -> str:
    if type(label) is not str or LOCAL_LABEL.fullmatch(label) is None:
        raise ValueError("local store label must match [a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
    return label


def _local_data_dir(location: object) -> str:
    if type(location) is not str or not location.strip():
        raise ValueError(f"{LOCAL_DATA_DIR_ENV} is required")
    location = location.strip()
    if not os.path.isabs(location) or any(ord(char) < 32 for char in location):
        raise ValueError(f"{LOCAL_DATA_DIR_ENV} must be an absolute path")
    if ".." in location.split("/"):
        raise ValueError(f"{LOCAL_DATA_DIR_ENV} must not contain '..'")
    return os.path.normpath(location)


def require_local_marker(data_dir: str, label: str) -> None:
    """Refuse a data directory that was not initialised for exactly this store.

    The marker guards against a bind mount that silently came up empty (the
    store would restart from nothing) and against two stores sharing a directory.
    """
    path = os.path.join(data_dir, LOCAL_MARKER_NAME)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("local data directory is not initialised (marker missing or unreadable)") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("local data directory marker must be a regular file")
        content = os.read(descriptor, _MARKER_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MARKER_MAX_BYTES:
        raise ValueError("local data directory marker is too large")
    try:
        record = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("local data directory marker is not valid JSON") from error
    if (type(record) is not dict or set(record) != {"format_version", "label"}
            or type(record["format_version"]) is not int or record["format_version"] != LOCAL_FORMAT_VERSION):
        raise ValueError("local data directory marker has an unknown format")
    if record["label"] != label:
        raise ValueError("local data directory belongs to another store")


def _clean_local_prefix(prefix: str) -> str:
    if type(prefix) is not str:
        raise ValueError("object prefix must be a string")
    clean = prefix.strip("/")
    if not clean:
        return ""
    parts = clean.split("/")
    if (any(part in ("", ".", "..") or part.strip() != part for part in parts)
            or "\\" in clean or any(ord(char) < 32 or ord(char) == 127 for char in clean)):
        raise ValueError("object prefix must be a canonical relative path")
    return clean


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value)[:48].strip("-.")
    return slug or "stream"


@dataclass(frozen=True)
class StorageBackend:
    kind: str
    raw_bucket: str
    state_location: str
    gcs_client: Any | None = None
    aws_client_factory: Callable[[str], Any] | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], *, gcs_client: Any | None = None,
                         aws_client_factory: Callable[[str], Any] | None = None) -> "StorageBackend":
        kind = backend_from_environment(environ)
        raw_bucket = _required(environ, "AS400_RAW_BUCKET")
        if kind == "local":
            label = validate_local_label(raw_bucket)
            data_dir = _local_data_dir(environ.get(LOCAL_DATA_DIR_ENV))
            require_local_marker(data_dir, label)
            return cls(kind, label, data_dir)
        if kind == "gcs":
            if gcs_client is None:
                from google.cloud import storage

                gcs_client = storage.Client()
            return cls(kind, raw_bucket, _required(environ, "AS400_CHECKPOINT_BUCKET"), gcs_client=gcs_client)
        return cls(kind, raw_bucket, _required(environ, "AS400_CHECKPOINT_TABLE"),
                   aws_client_factory=aws_client_factory)

    def _aws(self, service: str) -> Any | None:
        return self.aws_client_factory(service) if self.aws_client_factory is not None else None

    def _local_base(self) -> tuple[str, str]:
        """Validated ``(data_dir, label)``; the directory falls back to the process environment
        when the backend was built directly with an empty ``state_location``."""
        label = validate_local_label(self.raw_bucket)
        data_dir = _local_data_dir(self.state_location or os.environ.get(LOCAL_DATA_DIR_ENV))
        require_local_marker(data_dir, label)
        return data_dir, label

    def object_store(self, prefix: str):
        if self.kind == "local":
            from .object_store import FileObjectStore

            data_dir, label = self._local_base()
            root = os.path.join(data_dir, "objects", label)
            clean = _clean_local_prefix(prefix)
            return FileObjectStore(os.path.join(root, clean) if clean else root)
        if self.kind == "gcs":
            from .gcs_backend import GcsObjectStore

            return GcsObjectStore(self.raw_bucket, prefix, client=self.gcs_client)
        from .object_store import S3ObjectStore

        return S3ObjectStore(self.raw_bucket, prefix, client=self._aws("s3"))

    def checkpoint_store(self, stream_key: str):
        if self.kind == "local":
            from pathlib import Path

            from .checkpoint import JsonCheckpointStore

            if type(stream_key) is not str or not stream_key:
                raise ValueError("checkpoint stream key is required")
            digest = hashlib.sha256(stream_key.encode("utf-8")).hexdigest()[:16]
            directory = self._local_state_directory("checkpoints")
            return JsonCheckpointStore(Path(directory) / f"{_slug(stream_key)}-{digest}.json")
        if self.kind == "gcs":
            from .gcs_backend import GcsCheckpointStore

            return GcsCheckpointStore(self.state_location, stream_key, client=self.gcs_client)
        from .checkpoint import DynamoDbCheckpointStore

        return DynamoDbCheckpointStore(self.state_location, stream_key, client=self._aws("dynamodb"))

    def source_gate(self, gate_key: str, *, policy: Any | None = None):
        if self.kind == "local":
            from pathlib import Path

            from .source_gate import FileSourceGate

            if type(gate_key) is not str or not gate_key:
                raise ValueError("source gate key is required")
            digest = hashlib.sha256(gate_key.encode("utf-8")).hexdigest()
            directory = self._local_state_directory("source-gates")
            return FileSourceGate(Path(directory) / f"{digest}.json", policy=policy)
        if self.kind == "gcs":
            from .gcs_backend import GcsSourceGate

            return GcsSourceGate(self.state_location, gate_key, policy=policy, client=self.gcs_client)
        from .source_gate import DynamoDbSourceGate

        return DynamoDbSourceGate(self.state_location, gate_key, policy=policy, client=self._aws("dynamodb"))

    def _local_state_directory(self, name: str) -> str:
        from pathlib import Path

        from .object_store import _ensure_durable_directory

        data_dir, label = self._local_base()
        directory = os.path.join(data_dir, "state", label, name)
        _ensure_durable_directory(Path(directory))
        return directory


def probe_local_filesystem(data_dir: str, *, mountinfo_path: str = "/proc/self/mountinfo") -> dict[str, Any]:
    """Prove that ``data_dir`` offers the primitives the local backend relies on.

    Checks, each recorded as ``{"ok": bool, "detail": str}`` (never a path or secret):
    marker, filesystem type (denylist, informational when the platform has no
    mountinfo), hard link create-if-absent (second link must raise
    ``FileExistsError``), exclusive non-blocking ``flock`` conflict between two
    open file descriptions, atomic ``replace`` and ``fsync`` of the directory
    chain. Probe files live in a private temporary subdirectory that is always
    removed.
    """
    import fcntl

    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, detail: str) -> bool:
        checks[name] = {"ok": bool(ok), "detail": detail}
        return bool(ok)

    data_dir = _local_data_dir(data_dir)
    if not os.path.isdir(data_dir):
        record("data_dir", False, "data directory does not exist")
        return {"ok": False, "checks": checks}
    record("data_dir", True, "data directory present")
    try:
        descriptor = os.open(os.path.join(data_dir, LOCAL_MARKER_NAME), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            content = os.read(descriptor, _MARKER_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        label = json.loads(content.decode("utf-8")).get("label")
        validate_local_label(label)
        require_local_marker(data_dir, label)
        record("marker", True, "marker present for one store")
    except (OSError, ValueError, AttributeError) as error:
        record("marker", False, f"marker invalid: {type(error).__name__}")

    fstype = _filesystem_type(data_dir, mountinfo_path)
    if fstype is None:
        record("filesystem", True, "filesystem type unknown on this platform; behaviour probes decide")
    elif fstype in UNSUPPORTED_FILESYSTEMS or fstype.startswith("fuse"):
        record("filesystem", False, f"unsupported filesystem type {fstype}")
    else:
        record("filesystem", True, f"filesystem type {fstype}")

    work = os.path.join(data_dir, f".fs-probe-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        os.mkdir(work, 0o700)
    except OSError as error:
        record("writable", False, f"cannot create a probe directory: {type(error).__name__}")
        return {"ok": False, "checks": checks}
    record("writable", True, "probe directory created")
    created: list[str] = []
    try:
        _probe_hard_link(work, created, record)
        _probe_flock(work, created, record, fcntl)
        _probe_atomic_replace(work, created, record)
        _probe_fsync_chain(work, data_dir, record)
    finally:
        for name in created:
            try:
                os.unlink(os.path.join(work, name))
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks}


def _filesystem_type(path: str, mountinfo_path: str) -> str | None:
    try:
        with open(mountinfo_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in lines:
        head, separator, tail = line.partition(" - ")
        fields = head.split(" ")
        rest = tail.split(" ")
        if not separator or len(fields) < 5 or not rest or not rest[0]:
            continue
        mount_point = fields[4].replace("\\040", " ")
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) >= best[0]:
                best = (len(mount_point), rest[0].lower())
    return best[1] if best else None


def _probe_hard_link(work: str, created: list[str], record) -> None:
    source = os.path.join(work, "link-source")
    target = os.path.join(work, "link-target")
    try:
        with open(source, "wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        created.extend(["link-source", "link-target"])
        os.link(source, target)
        try:
            os.link(source, target)
        except FileExistsError:
            record("hard_link", True, "second link to an existing name is refused")
        else:
            record("hard_link", False, "second link to an existing name was accepted")
    except OSError as error:
        record("hard_link", False, f"hard link failed: {type(error).__name__}")


def _probe_flock(work: str, created: list[str], record, fcntl) -> None:
    path = os.path.join(work, "lock")
    first = second = None
    try:
        first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        created.append("lock")
        second = os.open(path, os.O_RDWR)
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(first, fcntl.LOCK_UN)
            try:
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                record("flock", False, f"lock not released: {type(error).__name__}")
            else:
                record("flock", True, "exclusive lock conflicts between open descriptions")
        else:
            record("flock", False, "two exclusive locks were granted at once")
    except OSError as error:
        record("flock", False, f"flock failed: {type(error).__name__}")
    finally:
        for descriptor in (first, second):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _probe_atomic_replace(work: str, created: list[str], record) -> None:
    target = os.path.join(work, "replace-target")
    staged = os.path.join(work, "replace-staged")
    try:
        for path, body in ((target, b"old"), (staged, b"new")):
            with open(path, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        created.extend(["replace-target", "replace-staged"])
        os.replace(staged, target)
        with open(target, "rb") as handle:
            content = handle.read()
        if content == b"new" and not os.path.exists(staged):
            record("atomic_replace", True, "replace swaps the content in one step")
        else:
            record("atomic_replace", False, "replace did not swap the content")
    except OSError as error:
        record("atomic_replace", False, f"replace failed: {type(error).__name__}")


def _probe_fsync_chain(work: str, data_dir: str, record) -> None:
    try:
        chain = []
        current = os.path.realpath(work)
        stop = os.path.realpath(data_dir)
        while True:
            chain.append(current)
            if current == stop or current == os.path.dirname(current):
                break
            current = os.path.dirname(current)
        for directory in chain:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        record("fsync_directories", True, "directory fsync accepted")
    except OSError as error:
        record("fsync_directories", False, f"directory fsync failed: {type(error).__name__}")
