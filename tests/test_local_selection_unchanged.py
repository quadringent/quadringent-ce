"""Adding the local backend leaves aws, gcs and Kubernetes selection as it was."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from local_support import LABEL, local_env, make_data_dir
from quadringent.capture_binding import CaptureBinding, _AUTHORITY
from quadringent.object_store import FileObjectStore, S3ObjectStore
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.storage_backend import StorageBackend, backend_from_environment, registry_authority

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")


def _load_script(name):
    spec = importlib.util.spec_from_file_location("local_selection_" + name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def capture(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return _load_script("as400_continuous_capture")


def _binding(authority, scope="reader-source-raw-v1-offline"):
    point = BoundCheckpoint(scope, "epoch", IDENTITY, 20)
    return CaptureBinding(scope, "epoch", "ibmi", "TESTJRN", authority, point)


# -- authority strings -----------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [
    ("aws", "s3://bucket-a/receiver-authority-v1"),
    ("s3", "s3://bucket-a/receiver-authority-v1"),
    ("gcs", "gcs://bucket-a/receiver-authority-v1"),
])
def test_remote_authorities_are_byte_identical_to_the_historical_spelling(kind, expected):
    assert registry_authority(kind, "bucket-a") == expected


def test_authority_regex_accepts_s3_gcs_local_and_rejects_file():
    for scheme in ("s3", "gcs", "local"):
        assert _AUTHORITY.fullmatch(f"{scheme}://{LABEL}/receiver-authority-v1")
    for rejected in ("file://qdt-0a1b2c3d/receiver-authority-v1", "file:///data/receiver-authority-v1",
                     "http://qdt-0a1b2c3d/receiver-authority-v1", "local://x/receiver-authority-v1",
                     "local://qdt-0a1b2c3d/other", "local:/qdt-0a1b2c3d/receiver-authority-v1"):
        assert _AUTHORITY.fullmatch(rejected) is None


def test_a_capture_binding_can_carry_the_local_authority_but_never_file():
    assert _binding(registry_authority("local", LABEL)).registry_authority.startswith("local://")
    with pytest.raises(ValueError):
        _binding(f"file://{LABEL}/receiver-authority-v1")


@pytest.mark.parametrize("backend,bucket,authority", [
    ("aws", "bucket-a", "s3://bucket-a/receiver-authority-v1"),
    ("gcs", "bucket-b", "gcs://bucket-b/receiver-authority-v1"),
    ("local", LABEL, f"local://{LABEL}/receiver-authority-v1"),
])
def test_installed_capture_binding_checks_the_backend_authority(capture, backend, bucket, authority):
    environ = {"QUADRINGENT_STORAGE_BACKEND": backend, "AS400_RAW_BUCKET": bucket,
               "AS400_STREAM_KEY": "reader-source-raw-v1-offline", "AS400_JOURNAL_LIBRARY": "TESTLIB",
               "AS400_JOURNAL_NAME": "TESTJRN",
               "AS400_CAPTURE_BINDING_JSON": json.dumps(_binding(authority).to_dict())}
    assert capture._installed_capture_binding(environ) == _binding(authority)
    other = dict(environ, AS400_CAPTURE_BINDING_JSON=json.dumps(
        _binding(authority.replace("://", "://x")).to_dict()))
    with pytest.raises(ValueError, match="differs from its original capture binding"):
        capture._installed_capture_binding(other)


def test_installed_capture_binding_keeps_remote_authorities_distinct(capture):
    environ = {"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_BUCKET": "bucket-a",
               "AS400_STREAM_KEY": "reader-source-raw-v1-offline", "AS400_JOURNAL_LIBRARY": "TESTLIB",
               "AS400_JOURNAL_NAME": "TESTJRN",
               "AS400_CAPTURE_BINDING_JSON": json.dumps(_binding("s3://bucket-a/receiver-authority-v1").to_dict())}
    with pytest.raises(ValueError):
        capture._installed_capture_binding(environ)


# -- reader scope ---------------------------------------------------------------------------


def _legacy_stream_key(backend):
    backend = {"aws": "s3", "s3": "s3", "gcs": "gcs"}[backend]
    values = (backend, "src-1", "TESTLIB", "TESTJRN", "bucket-a", "raw/example")
    identity = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()
    return "reader-source-raw-v1-" + hashlib.sha256(identity).hexdigest()


@pytest.mark.parametrize("backend", ["aws", "s3", "gcs"])
def test_reader_stream_keys_for_remote_backends_are_unchanged(backend):
    assert reader_checkpoint_stream_key(
        storage_backend=backend, source_id="src-1", journal_library="TESTLIB", journal_name="TESTJRN",
        raw_bucket="bucket-a", raw_prefix="raw/example") == _legacy_stream_key(backend)


def test_reader_stream_key_for_local_is_its_own_namespace():
    arguments = dict(source_id="src-1", journal_library="TESTLIB", journal_name="TESTJRN",
                     raw_bucket="bucket-a", raw_prefix="raw/example")
    local = reader_checkpoint_stream_key(storage_backend="local", **arguments)
    assert local not in {_legacy_stream_key("aws"), _legacy_stream_key("gcs")}
    assert local == reader_checkpoint_stream_key(storage_backend=" LOCAL ", **arguments)
    with pytest.raises(ValueError):
        reader_checkpoint_stream_key(storage_backend="file", **arguments)


# -- storage selection ----------------------------------------------------------------------


def test_aws_selection_is_unchanged():
    clients = []
    backend = StorageBackend.from_environment(
        {"AS400_RAW_BUCKET": "bucket-a", "AS400_CHECKPOINT_TABLE": "table-a"},
        aws_client_factory=lambda service: clients.append(service) or object())
    assert (backend.kind, backend.raw_bucket, backend.state_location) == ("aws", "bucket-a", "table-a")
    assert isinstance(backend.object_store("p"), S3ObjectStore)
    assert backend.object_store("p").prefix == "p"
    assert clients == ["s3", "s3"]
    with pytest.raises(ValueError, match="AS400_CHECKPOINT_TABLE"):
        StorageBackend.from_environment({"AS400_RAW_BUCKET": "bucket-a"})


def test_gcs_selection_is_unchanged():
    from quadringent.gcs_backend import GcsCheckpointStore, GcsObjectStore
    from test_gcs_backend import FakeGcsClient

    client = FakeGcsClient()
    backend = StorageBackend.from_environment(
        {"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_BUCKET": "bucket-a", "AS400_CHECKPOINT_BUCKET": "state-a"},
        gcs_client=client)
    assert (backend.kind, backend.state_location) == ("gcs", "state-a")
    assert isinstance(backend.object_store("p"), GcsObjectStore)
    assert isinstance(backend.checkpoint_store("stream"), GcsCheckpointStore)
    with pytest.raises(ValueError, match="AS400_CHECKPOINT_BUCKET"):
        StorageBackend.from_environment({"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_BUCKET": "bucket-a"},
                                        gcs_client=client)


def test_remote_selection_never_touches_the_local_environment(monkeypatch):
    monkeypatch.setenv("AS400_LOCAL_DATA_DIR", "/definitely/not/used")
    backend = StorageBackend(kind="aws", raw_bucket="bucket-a", state_location="table-a", aws_client_factory=lambda s: object())
    assert isinstance(backend.object_store("p"), S3ObjectStore)


def test_capture_script_factories_choose_local_first_and_keep_remote_branches(capture, monkeypatch, tmp_path):
    data = make_data_dir(tmp_path)
    for key, value in local_env(data).items():
        monkeypatch.setenv(key, value)
    assert isinstance(capture._object_store("local", "raw/t"), FileObjectStore)
    assert capture._checkpoint_store("local", "stream-1").path.parent == data / "state" / LABEL / "checkpoints"
    assert capture._source_gate("local", "source-gate#h#U").path.parent == data / "state" / LABEL / "source-gates"
    monkeypatch.setenv("AS400_RAW_BUCKET", "bucket-a")
    monkeypatch.setattr(capture, "S3ObjectStore", lambda bucket, prefix: ("s3", bucket, prefix))
    assert capture._object_store("aws", "raw/t") == ("s3", "bucket-a", "raw/t")


def test_remote_only_capture_features_stay_aws_only(capture):
    source = (ROOT / "scripts" / "as400_continuous_capture.py").read_text()
    assert 'if backend != "aws" and (args.reserve_run_id is not None' in source


# -- Kubernetes manifests --------------------------------------------------------------------


def test_reader_manifest_builder_still_refuses_a_fully_consistent_local_binding():
    from datetime import datetime, timezone

    from quadringent_control_plane.v2.executor.boundary import JournalBoundary
    from quadringent_control_plane.v2.executor.manifests import (
        ManifestError, TableBootstrap, build_reader_deployment)
    from test_v2_executor_manifest_script_contracts import _reader_spec

    scope = reader_checkpoint_stream_key(storage_backend="local", source_id="src-1", journal_library="TESTLIB",
                                         journal_name="TESTJRN", raw_bucket=LABEL, raw_prefix="raw/example")
    binding = _binding(registry_authority("local", LABEL), scope)
    boundary = JournalBoundary("TESTLIB", "RCV0001", 20, datetime(2026, 10, 13, tzinfo=timezone.utc),
                               receiver_identity=IDENTITY).with_binding(binding)
    table = TableBootstrap("tbl-orders", "TESTLIB", "QDC_ORDERS", boundary)
    with pytest.raises(ManifestError):
        build_reader_deployment(_reader_spec(storage_backend="local", raw_bucket=LABEL, tables=(table,)))


def test_reader_manifest_builder_refuses_the_local_backend_even_unbound():
    from datetime import datetime, timezone

    from quadringent_control_plane.v2.executor.boundary import JournalBoundary
    from quadringent_control_plane.v2.executor.manifests import (
        ManifestError, TableBootstrap, build_reader_deployment)
    from test_v2_executor_manifest_script_contracts import _reader_spec

    boundary = JournalBoundary("TESTLIB", "RCV0001", 20, datetime(2026, 10, 13, tzinfo=timezone.utc),
                               receiver_identity=IDENTITY)
    table = TableBootstrap("tbl-orders", "TESTLIB", "QDC_ORDERS", boundary)
    with pytest.raises(ManifestError, match="only for quadringent local"):
        build_reader_deployment(_reader_spec(storage_backend="local", raw_bucket=LABEL, tables=(table,)))


def test_loader_authority_helper_matches_the_historical_rule():
    loader = _load_script("quadringent_destination_loader")
    import types

    for kind, bucket, authority in (("aws", "bucket-a", "s3://bucket-a/receiver-authority-v1"),
                                    ("gcs", "bucket-a", "gcs://bucket-a/receiver-authority-v1")):
        table = types.SimpleNamespace(capture_binding=types.SimpleNamespace(
            registry_authority=authority, source_system="ibmi", journal="TESTJRN"))
        storage = types.SimpleNamespace(kind=kind, raw_bucket=bucket, object_store=lambda prefix: object())
        assert loader._bound_registry(table, storage).authority == authority
        table.capture_binding.registry_authority = "s3://other/receiver-authority-v1"
        with pytest.raises(loader.DestinationLoaderError):
            loader._bound_registry(table, storage)
    unknown = types.SimpleNamespace(kind="file", raw_bucket="bucket-a", object_store=lambda prefix: object())
    table = types.SimpleNamespace(capture_binding=types.SimpleNamespace(
        registry_authority="s3://bucket-a/receiver-authority-v1", source_system="ibmi", journal="TESTJRN"))
    with pytest.raises(loader.DestinationLoaderError):
        loader._bound_registry(table, unknown)
