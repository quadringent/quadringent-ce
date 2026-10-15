"""Snapshot copy -> journal capture -> loader on the local backend, with a fake journal.

Every stage runs the production code over one real ``StorageBackend`` (kind ``local``):
the in-image admission and copy commands, the raw-first capture coordinator, and the
destination loader against a recording cursor. Only IBM i (a fake receiver catalog and a
fake snapshot writer) and Snowflake (a ``Mock`` cursor) are replaced.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

import quadringent_control_plane  # noqa: F401  (the package must win over scripts/quadringent_control_plane.py)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import quadringent_destination_loader as loader  # noqa: E402
import quadringent_initial_copy_job as copy_job  # noqa: E402

from local_support import LABEL, local_env, make_data_dir  # noqa: E402
from quadringent.continuous import ReceiverSnapshot  # noqa: E402
from quadringent.contract import ChangeEvent, JournalPosition  # noqa: E402
from quadringent.object_store import RawFirstCaptureCoordinator  # noqa: E402
from quadringent.raw import RawBatchWriter, bind_raw_batch  # noqa: E402
from quadringent.receiver_identity import ReceiverIdentity  # noqa: E402
from quadringent.storage_backend import StorageBackend  # noqa: E402
from quadringent.storage_layout import journal_prefix, snapshot_prefix  # noqa: E402
from quadringent_control_plane.v2.executor import local_runtime as runtime  # noqa: E402
from quadringent_control_plane.v2.executor.boundary import JournalBoundary  # noqa: E402
from quadringent_control_plane.v2.executor.evidence import EvidenceReader  # noqa: E402

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
SCHEMA, TABLE, JOURNAL = "LEDGER", "CNTR", "TESTJRN"
RAW_ROOT = "raw/example"
IDENTITY = ReceiverIdentity("TESTLIB", JOURNAL, "TESTLIB", "RCV0001", "native-test-v1", "generation-a")


def event(operation: str, sequence: int, *, receiver="RCV0001", journal=JOURNAL, row=None) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi", journal=journal, library=SCHEMA, table=TABLE, operation=operation,
        position=JournalPosition(receiver, sequence), commit_timestamp="2026-10-13T09:31:00Z",
        schema_version="sha256:test", before=None, after=row or {"ID": sequence})


class FakeNativeCatalog:
    """The ATTACHED receiver still covers the admitted boundary."""

    def __init__(self, **_kwargs):
        pass

    def identity(self, **_kwargs):
        return IDENTITY

    def snapshot(self, required_receiver=None):
        return [ReceiverSnapshot("TESTLIB", "RCV0001", 1, 4300, "ATTACHED")]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    data = make_data_dir(tmp_path)
    env = local_env(data)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    storage = StorageBackend.from_environment(env)
    observed = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY)
    admission = runtime.admit({
        "source_id": "src-1", "pipeline_id": "pipe-1", "table_id": "tbl-cntr", "journal_library": "TESTLIB",
        "journal_name": JOURNAL, "raw_prefix": RAW_ROOT, "boundary": runtime.observed_boundary_to_dict(observed)},
        storage)
    return storage, env, admission


def fake_snapshot_writer(run_id: str, rows: int):
    def run_snapshot(config, *, base_environ, runner=None):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        position = JournalPosition("SNAPSHOT:" + run_id, 1)
        events = [ChangeEvent(
            source_system="ibmi", journal=f"SNAPSHOT:{SCHEMA}.{TABLE}", library=SCHEMA, table=TABLE, operation="c",
            position=JournalPosition(position.receiver, index + 1), commit_timestamp="2026-10-13T09:30:00Z",
            schema_version="sha256:test", before=None, after={"ID": 9000 + index}) for index in range(rows)]
        RawBatchWriter(str(config.output_dir)).write_batch(events, high_watermark=events[-1].position)
        return rows
    return run_snapshot


def run_copy(storage, env, admission, monkeypatch, *, rows=2):
    monkeypatch.setattr(copy_job, "run_snapshot", fake_snapshot_writer(admission["run_id"], rows))
    environ = dict(env, ISERIES_HOST="192.0.2.10", ISERIES_USER="TESTUSER", AS400_JAVA_CLASSPATH="offline.jar")
    boundary = JournalBoundary.from_dict(admission["boundary"])
    return runtime.run_copy(storage, admission, boundary, boundary.capture_binding, schema=SCHEMA, table=TABLE,
                            password="offline-password", environ=environ, catalog_factory=FakeNativeCatalog,
                            now=lambda: NOW)


def loader_table(admission):
    return loader.LoaderTable("tbl-cntr", SCHEMA, TABLE, ("ID",), ({"name": "ID", "kind": "integer", "nullable": False},),
                              admission["evidence_key"], "pipe-1", admission["run_id"],
                              JournalBoundary.from_dict(admission["boundary"]).capture_binding)


def capture_one_window(storage, admission, *, sequence=4205, end=4210):
    binding = JournalBoundary.from_dict(admission["boundary"]).capture_binding
    checkpoint = storage.checkpoint_store(admission["stream_key"])
    checkpoint.compare_and_set_bound(None, binding.bootstrap)
    store = storage.object_store(journal_prefix(RAW_ROOT, TABLE))
    staging = Path(storage.state_location) / "staging"
    manifest = RawBatchWriter(str(staging)).write_batch([event("c", sequence, row={"ID": 1})],
                                                        high_watermark=JournalPosition("RCV0001", end))
    manifest_bytes = (staging / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
    payload = (staging / f"batch-{manifest.batch_id}.jsonl").read_bytes()
    stop = binding.checkpoint(IDENTITY, end)
    manifest_bytes = bind_raw_batch(manifest_bytes, payload, binding=binding, end=stop)
    RawFirstCaptureCoordinator(store, checkpoint).capture_receipted_window_result(
        start=binding.checkpoint(IDENTITY, binding.bootstrap.sequence + 1), end=stop, previous=binding.bootstrap, manifest_content=manifest_bytes,
        payload=payload, scan_completed_at=NOW, capture_binding=binding)
    return stop


def test_copy_runs_the_unchanged_job_on_local_storage(world, monkeypatch):
    storage, env, admission = world
    result = run_copy(storage, env, admission, monkeypatch)
    assert result == {"evidence_key": admission["evidence_key"], "rows": 2}
    evidence = EvidenceReader(storage.object_store("")).read(admission["evidence_key"])
    assert evidence.rows_copied == 2 and evidence.require_bound() == JournalBoundary.from_dict(
        admission["boundary"]).require_bound()
    assert len(evidence.snapshot_batches) == 1
    snapshot_store = storage.object_store(snapshot_prefix(RAW_ROOT, TABLE))
    for reference in evidence.snapshot_batches:
        assert snapshot_store.get(reference.payload_key) and snapshot_store.get(reference.manifest_key)
    assert not (Path(storage.state_location) / "scratch" / admission["run_id"]).exists()


def test_a_finished_run_is_not_copied_twice(world, monkeypatch):
    storage, env, admission = world
    run_copy(storage, env, admission, monkeypatch, rows=2)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("the snapshot reader ran again")

    monkeypatch.setattr(copy_job, "run_snapshot", must_not_run)
    boundary = JournalBoundary.from_dict(admission["boundary"])
    again = runtime.run_copy(storage, admission, boundary, boundary.capture_binding, schema=SCHEMA, table=TABLE,
                             password="offline-password", environ=dict(env))
    assert again == {"evidence_key": admission["evidence_key"], "rows": 2}


def test_a_failed_attempt_leaves_no_evidence_and_can_be_retried(world, monkeypatch):
    storage, env, admission = world

    def broken(config, *, base_environ, runner=None):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "partial.tmp").write_text("half a snapshot")
        raise copy_job.InitialCopyJobError("JDBC snapshot failed (exit code 1) : offline-password in a stack trace")

    monkeypatch.setattr(copy_job, "run_snapshot", broken)
    boundary = JournalBoundary.from_dict(admission["boundary"])
    environ = dict(env, ISERIES_HOST="192.0.2.10", ISERIES_USER="TESTUSER", AS400_JAVA_CLASSPATH="offline.jar")
    with pytest.raises(runtime.LocalRuntimeError) as raised:
        runtime.run_copy(storage, admission, boundary, boundary.capture_binding, schema=SCHEMA, table=TABLE,
                         password="offline-password", environ=environ, catalog_factory=FakeNativeCatalog)
    assert "offline-password" not in runtime.redact(str(raised.value), ["offline-password"])
    assert EvidenceReader(storage.object_store("")).read(admission["evidence_key"]) is None
    assert run_copy(storage, env, admission, monkeypatch)["rows"] == 2  # the partial directory was cleared


def test_the_loader_applies_snapshot_then_capture_and_restarts_idempotently(world, monkeypatch):
    storage, env, admission = world
    run_copy(storage, env, admission, monkeypatch)
    end = capture_one_window(storage, admission)
    table = loader_table(admission)
    plan = loader.build_plan(table, database="D", schema="S")
    cursor = Mock(rowcount=1)
    cursor.fetchall.return_value = []
    # a fresh StorageBackend per stage, as separate containers would have
    loader_storage = StorageBackend.from_environment(env)
    loader.bind_loader_scopes(storage=loader_storage, tables=(table,), raw_prefix_root=RAW_ROOT, account="ACME-TEST",
                              database="D", schema="S", mirror_schema="S")
    first = loader.load_table_once(table, plan=plan, storage=loader_storage, raw_prefix_root=RAW_ROOT,
                                   streaming_client_factory=None, cursor=cursor, history_mode="sql")
    assert first[1] >= 1
    checkpoint = loader_storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    assert checkpoint.load_bound() == end
    statements = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "DELETE FROM" in statements or "MERGE" in statements or "INSERT" in statements
    cursor.reset_mock()
    restarted = StorageBackend.from_environment(env)
    assert loader.load_table_once(table, plan=plan, storage=restarted, raw_prefix_root=RAW_ROOT,
                                  streaming_client_factory=None, cursor=cursor, history_mode="sql") == (0, 0)
    cursor.execute.assert_not_called()


def test_the_loader_does_not_adopt_another_store(world, monkeypatch, tmp_path):
    storage, env, admission = world
    run_copy(storage, env, admission, monkeypatch)
    (tmp_path / "other").mkdir()
    other = StorageBackend.from_environment(local_env(make_data_dir(tmp_path / "other", "qdt-other-store"),
                                                      "qdt-other-store"))
    table = loader_table(admission)
    # the capture binding names the authority of the first store only
    with pytest.raises(loader.DestinationLoaderError, match="registry authority"):
        loader._bound_registry(table, other)
    assert loader._bound_registry(table, storage).authority == f"local://{LABEL}/receiver-authority-v1"
    cursor = Mock()
    plan = loader.build_plan(table, database="D", schema="S")
    assert loader.load_table_once(table, plan=plan, storage=other, raw_prefix_root=RAW_ROOT,
                                  streaming_client_factory=None, cursor=cursor, history_mode="sql") == (0, 0)
    cursor.execute.assert_not_called()
