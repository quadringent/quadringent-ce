"""The local storage backend: selection, layout, markers and the filesystem probe."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

import pytest

from quadringent import storage_backend as sb
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.object_store import FileObjectStore
from quadringent.source_gate import FileSourceGate, SourceGatePolicy
from quadringent.storage_backend import (
    BACKENDS,
    StorageBackend,
    backend_from_environment,
    probe_local_filesystem,
    registry_authority,
)

from local_storage_fakes import LABEL, bound_point, init_data_dir


@pytest.fixture()
def env(tmp_path):
    return init_data_dir(tmp_path / "data")


def test_local_is_a_known_backend_and_other_names_are_still_refused():
    assert "local" in BACKENDS and BACKENDS[:2] == ("aws", "gcs")
    assert backend_from_environment({"QUADRINGENT_STORAGE_BACKEND": " LOCAL "}) == "local"
    assert backend_from_environment({}) == "aws"
    for refused in ("file", "s3", "azure"):
        with pytest.raises(ValueError, match="aws, gcs or local"):
            backend_from_environment({"QUADRINGENT_STORAGE_BACKEND": refused})


def test_from_environment_builds_a_local_backend(env):
    storage = StorageBackend.from_environment(env)
    assert (storage.kind, storage.raw_bucket, storage.state_location) == ("local", LABEL, env["AS400_LOCAL_DATA_DIR"])
    assert storage.gcs_client is None and storage.aws_client_factory is None


@pytest.mark.parametrize("label", ["Upper-case", "a", "ab", "-leading", "trailing-", "has space", "x" * 223, "a/b-c1"])
def test_labels_outside_the_store_id_pattern_are_refused(env, label):
    env["AS400_RAW_BUCKET"] = label
    with pytest.raises(ValueError, match="label"):
        StorageBackend.from_environment(env)


@pytest.mark.parametrize("label", ["qdt-0a1b2c3d", "a1b", "x" * 222, "a.b_c-d9"])
def test_labels_inside_the_pattern_are_accepted(tmp_path, label):
    assert StorageBackend.from_environment(init_data_dir(tmp_path / "d", label)).raw_bucket == label


def test_data_directory_must_be_absolute_and_present(env, tmp_path):
    missing = dict(env)
    del missing["AS400_LOCAL_DATA_DIR"]
    with pytest.raises(ValueError, match="AS400_LOCAL_DATA_DIR"):
        StorageBackend.from_environment(missing)
    for bad in ("relative/dir", "/data/../etc"):
        with pytest.raises(ValueError, match="absolute|'..'"):
            StorageBackend.from_environment({**env, "AS400_LOCAL_DATA_DIR": bad})
    with pytest.raises(ValueError, match="not initialised"):
        StorageBackend.from_environment({**env, "AS400_LOCAL_DATA_DIR": str(tmp_path / "absent")})


def test_missing_wrong_or_malformed_markers_are_refused(env, tmp_path):
    data = Path(env["AS400_LOCAL_DATA_DIR"])
    marker = data / ".quadringent-local"
    marker.write_text(json.dumps({"format_version": 1, "label": "qdt-other-store"}))
    with pytest.raises(ValueError, match="another store"):
        StorageBackend.from_environment(env)
    for content, message in (("not json", "not valid JSON"),
                             (json.dumps({"format_version": 2, "label": LABEL}), "unknown format"),
                             (json.dumps({"format_version": 1, "label": LABEL, "extra": 1}), "unknown format"),
                             (json.dumps({"format_version": True, "label": LABEL}), "unknown format"),
                             (json.dumps([1]), "unknown format"),
                             ("x" * 5000, "too large")):
        marker.write_text(content)
        with pytest.raises(ValueError, match=message):
            StorageBackend.from_environment(env)
    marker.unlink()
    with pytest.raises(ValueError, match="not initialised"):
        StorageBackend.from_environment(env)


def test_a_symlinked_marker_is_refused(env, tmp_path):
    data = Path(env["AS400_LOCAL_DATA_DIR"])
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"format_version": 1, "label": LABEL}))
    (data / ".quadringent-local").unlink()
    (data / ".quadringent-local").symlink_to(real)
    with pytest.raises(ValueError, match="not initialised"):
        StorageBackend.from_environment(env)


def test_local_does_not_need_the_cloud_variables(env):
    assert "AS400_CHECKPOINT_TABLE" not in env and "AS400_CHECKPOINT_BUCKET" not in env
    StorageBackend.from_environment(env)


def test_object_store_is_a_file_store_below_the_label(env):
    storage = StorageBackend.from_environment(env)
    store = storage.object_store("site/orders/journal")
    assert isinstance(store, FileObjectStore)
    assert store.put_once("receipts/a.json", b"{}") is True
    expected = Path(env["AS400_LOCAL_DATA_DIR"]) / "objects" / LABEL / "site/orders/journal/receipts/a.json"
    assert expected.read_bytes() == b"{}"
    assert store.list_receipt_keys(10) == ("receipts/a.json",)
    assert storage.object_store("").put_once("site/orders/journal/receipts/a.json", b"{}") is False
    assert storage.object_store("/site/orders/journal/").get("receipts/a.json") == b"{}"


@pytest.mark.parametrize("prefix", ["../x", "a/../b", "a//b", "a/./b", "a\\b", "a/ b", "a/\x00b", "a/b\n"])
def test_prefixes_cannot_leave_the_label_directory(env, prefix):
    with pytest.raises(ValueError, match="canonical relative path"):
        StorageBackend.from_environment(env).object_store(prefix)


def test_object_keys_cannot_escape_the_store_root(env):
    store = StorageBackend.from_environment(env).object_store("a")
    for key in ("../escape", "/abs"):
        with pytest.raises(ValueError):
            store.put_once(key, b"x")


def test_two_labels_in_one_directory_never_share_objects(tmp_path):
    first = init_data_dir(tmp_path / "data", "qdt-first-store")
    second = dict(first, AS400_RAW_BUCKET="qdt-second-store")
    # the marker pins the directory to one label, so a second label is refused outright
    StorageBackend.from_environment(first).object_store("p").put_once("k", b"1")
    with pytest.raises(ValueError, match="another store"):
        StorageBackend.from_environment(second)


def test_checkpoint_store_path_and_round_trip(env):
    storage = StorageBackend.from_environment(env)
    stream = "reader-source-raw-v1-" + "ab" * 32
    checkpoint = storage.checkpoint_store(stream)
    assert isinstance(checkpoint, JsonCheckpointStore)
    assert checkpoint.load_bound() is None
    checkpoint.compare_and_set_bound(None, bound_point(7))
    digest = hashlib.sha256(stream.encode()).hexdigest()[:16]
    directory = Path(env["AS400_LOCAL_DATA_DIR"]) / "state" / LABEL / "checkpoints"
    files = sorted(path.name for path in directory.iterdir())
    assert re.fullmatch(rf"[A-Za-z0-9._-]+-{digest}\.json", files[0]), files
    assert files[1] == files[0] + ".lock"
    assert StorageBackend.from_environment(env).checkpoint_store(stream).load_bound() == bound_point(7)
    other = storage.checkpoint_store(stream + "x")
    assert other.path != checkpoint.path and other.load_bound() is None


def test_checkpoint_slug_is_safe_for_hostile_stream_keys(env):
    storage = StorageBackend.from_environment(env)
    hostile = "../../etc/passwd\x00 weird key/with:chars" + "z" * 300
    path = storage.checkpoint_store(hostile).path
    assert path.parent == Path(env["AS400_LOCAL_DATA_DIR"]) / "state" / LABEL / "checkpoints"
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,48}-[0-9a-f]{16}\.json", path.name), path.name
    with pytest.raises(ValueError):
        storage.checkpoint_store("")


def test_legacy_checkpoint_stays_unbound_like_the_cloud_stores(env):
    from quadringent.contract import JournalPosition

    checkpoint = StorageBackend.from_environment(env).checkpoint_store("legacy")
    checkpoint.compare_and_set(None, JournalPosition("RCV0001", 5))
    with pytest.raises(ValueError, match="unbound"):
        checkpoint.load_bound()


def test_source_gate_is_file_backed_and_shared(env):
    storage = StorageBackend.from_environment(env)
    key = "source-gate#ibmi.example.test#SVCUSER"
    policy = SourceGatePolicy()
    gate = storage.source_gate(key, policy=policy)
    assert isinstance(gate, FileSourceGate)
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    gate.before_connect(now=now)
    gate.record_connect_success(now=now)
    expected = Path(env["AS400_LOCAL_DATA_DIR"]) / "state" / LABEL / "source-gates" / (
        hashlib.sha256(key.encode()).hexdigest() + ".json")
    assert expected.is_file()
    assert StorageBackend.from_environment(env).source_gate(key).state()["format_version"] == \
        gate.state()["format_version"]
    with pytest.raises(ValueError):
        storage.source_gate("")


def test_state_location_falls_back_to_the_environment_when_built_directly(env, monkeypatch):
    direct = StorageBackend("local", LABEL, "")
    with pytest.raises(ValueError, match="AS400_LOCAL_DATA_DIR"):
        monkeypatch.delenv("AS400_LOCAL_DATA_DIR", raising=False)
        direct.object_store("p")
    monkeypatch.setenv("AS400_LOCAL_DATA_DIR", env["AS400_LOCAL_DATA_DIR"])
    assert direct.object_store("p").put_once("k", b"v") is True
    assert StorageBackend.from_environment(env).object_store("p").get("k") == b"v"


def test_direct_construction_still_checks_label_and_marker(env, monkeypatch):
    monkeypatch.setenv("AS400_LOCAL_DATA_DIR", env["AS400_LOCAL_DATA_DIR"])
    with pytest.raises(ValueError, match="another store"):
        StorageBackend("local", "qdt-other-store", "").object_store("")
    with pytest.raises(ValueError, match="label"):
        StorageBackend("local", "Bad Label", "").checkpoint_store("s")


def test_registry_authority_is_byte_identical_for_the_cloud_backends():
    assert registry_authority("aws", "bucket-one") == "s3://bucket-one/receiver-authority-v1"
    assert registry_authority("s3", "bucket-one") == "s3://bucket-one/receiver-authority-v1"
    assert registry_authority("gcs", "bucket-one") == "gcs://bucket-one/receiver-authority-v1"
    assert registry_authority("local", LABEL) == f"local://{LABEL}/receiver-authority-v1"
    # the historical inline expressions
    for backend, bucket in (("aws", "b-1"), ("gcs", "b-2")):
        assert registry_authority(backend, bucket) == f"{'gcs' if backend == 'gcs' else 's3'}://{bucket}/receiver-authority-v1"
    for unknown in ("file", None, ""):
        with pytest.raises(ValueError):
            registry_authority(unknown, "b")


def test_importing_the_module_pulls_no_posix_only_or_cloud_module():
    import subprocess
    import sys

    code = ("import sys; import quadringent.storage_backend; "
            "bad = [m for m in ('fcntl', 'boto3', 'google', 'quadringent.object_store', 'quadringent.source_gate') "
            "if m in sys.modules]; print(bad)")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(sb.__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# -- filesystem probe --------------------------------------------------------------------------------------


def mountinfo(tmp_path, entries):
    path = tmp_path / "mountinfo"
    lines = [f"{index + 20} 1 0:{index + 20} / {mount} rw - {fstype} {source} rw"
             for index, (mount, fstype, source) in enumerate(entries)]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_probe_passes_on_a_local_directory_and_cleans_up(env):
    data = env["AS400_LOCAL_DATA_DIR"]
    before = sorted(os.listdir(data))
    result = probe_local_filesystem(data)
    assert result["ok"] is True, result
    assert set(result["checks"]) >= {"data_dir", "marker", "filesystem", "writable", "hard_link", "flock",
                                    "atomic_replace", "fsync_directories"}
    assert all(item["ok"] for item in result["checks"].values())
    assert sorted(os.listdir(data)) == before
    assert data not in json.dumps(result)


def test_probe_requires_the_marker(tmp_path):
    result = probe_local_filesystem(init_data_dir(tmp_path / "d", marker=False)["AS400_LOCAL_DATA_DIR"])
    assert result["ok"] is False and result["checks"]["marker"]["ok"] is False


def test_probe_reports_a_missing_directory_without_raising(tmp_path):
    result = probe_local_filesystem(str(tmp_path / "absent"))
    assert result == {"ok": False, "checks": {"data_dir": {"ok": False, "detail": "data directory does not exist"}}}


@pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smb3", "9p", "fuse.sshfs", "fuseblk", "fuse",
                                    "tmpfs", "vfat", "exfat", "ntfs3", "drvfs"])
def test_unsupported_filesystem_types_fail_the_probe(env, tmp_path, fstype):
    data = os.path.realpath(env["AS400_LOCAL_DATA_DIR"])
    path = mountinfo(tmp_path, [("/", "ext4", "/dev/sda1"), (data, fstype, "server:/export")])
    result = probe_local_filesystem(data, mountinfo_path=path)
    assert result["ok"] is False
    assert result["checks"]["filesystem"]["ok"] is False and fstype in result["checks"]["filesystem"]["detail"]


@pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "overlay", "virtiofs", "apfs"])
def test_local_filesystem_types_pass_and_nested_mounts_win(env, tmp_path, fstype):
    data = os.path.realpath(env["AS400_LOCAL_DATA_DIR"])
    parent = os.path.dirname(data)
    path = mountinfo(tmp_path, [("/", "nfs", "server:/"), (parent, fstype, "/dev/x"), ("/elsewhere", "nfs", "s:/e")])
    result = probe_local_filesystem(data, mountinfo_path=path)
    assert result["checks"]["filesystem"] == {"ok": True, "detail": f"filesystem type {fstype}"}


def test_probe_without_mountinfo_relies_on_the_behaviour_checks(env, tmp_path):
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"], mountinfo_path=str(tmp_path / "none"))
    assert result["ok"] is True and "unknown" in result["checks"]["filesystem"]["detail"]


def test_probe_fails_when_a_second_hard_link_is_accepted(env, monkeypatch):
    real = os.link
    calls = {}

    def lenient(source, target, *args, **kwargs):
        calls[target] = calls.get(target, 0) + 1
        if calls[target] > 1:
            return None
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", lenient)
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"])
    assert result["ok"] is False and result["checks"]["hard_link"]["ok"] is False


def test_probe_fails_when_hard_links_are_unsupported(env, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse)
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"])
    assert result["checks"]["hard_link"]["ok"] is False and "PermissionError" in result["checks"]["hard_link"]["detail"]


def test_probe_fails_when_two_exclusive_locks_are_granted(env, monkeypatch):
    import fcntl

    monkeypatch.setattr(fcntl, "flock", lambda *args, **kwargs: None)
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"])
    assert result["ok"] is False and result["checks"]["flock"]["ok"] is False


def test_probe_fails_when_replace_is_not_atomic_swap(env, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda source, target: None)
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"])
    assert result["checks"]["atomic_replace"]["ok"] is False


def test_probe_fails_when_a_directory_cannot_be_synced(env, monkeypatch):
    real = os.fsync
    state = {"directory": False}
    real_open = os.open

    def tracking_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if os.path.isdir(path):
            state["directory"] = descriptor
        return descriptor

    def failing_fsync(descriptor):
        if descriptor == state["directory"]:
            raise OSError(22, "Invalid argument")
        return real(descriptor)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    result = probe_local_filesystem(env["AS400_LOCAL_DATA_DIR"])
    assert result["checks"]["fsync_directories"]["ok"] is False


def test_probe_removes_its_directory_even_when_a_check_fails(env, monkeypatch):
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(95, "no")))
    data = env["AS400_LOCAL_DATA_DIR"]
    before = sorted(os.listdir(data))
    probe_local_filesystem(data)
    assert sorted(os.listdir(data)) == before
