from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

import quadringent_destination_loader as loader
from quadringent.stage_timings import StageTrace
from test_bound_destination_loader import setup, receipt
from test_bound_proof_windows import BOOTSTRAP


class Clock:
    elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def utc_now(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed)

    def wait(self, seconds):
        self.elapsed += seconds


class Cursor:
    rowcount = 1
    sfqid = None

    def __init__(self, clock, failure=None):
        self.clock, self.failure = clock, failure
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        self.sfqid = f"00000000-0000-0000-0000-aaaaaaaaaa{len(self.statements):02x}"
        if statement.startswith("MERGE") and "CNTR_HISTORY" in statement.split("USING", 1)[0]:
            self.clock.wait(2)
            if self.failure:
                raise self.failure
        elif statement.startswith("MERGE"):
            self.clock.wait(3)
        return self

    def fetchall(self):
        return []


def prepare(tmp_path, monkeypatch, *, initialized=True, failure=None, emit=None):
    storage, table, _ = setup(tmp_path)
    end = receipt(storage, tmp_path)
    checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    if initialized:
        checkpoint.compare_and_set_bound(None, BOOTSTRAP)
    clock, records = Clock(), []
    trace = StageTrace("loader", binding=table.capture_binding, table_id=table.table_id,
                       emit=emit or records.append, monotonic=clock.monotonic, utc_now=clock.utc_now)
    monkeypatch.setattr(loader, "stage_trace_from_environment", lambda *args, **kwargs: trace)
    preflight = loader._bound_preflight

    def measured_preflight(*args, **kwargs):
        clock.wait(.5)
        return preflight(*args, **kwargs)

    monkeypatch.setattr(loader, "_bound_preflight", measured_preflight)
    cas = type(checkpoint).compare_and_set_bound

    def measured_cas(self, *args, **kwargs):
        clock.wait(.2)
        return cas(self, *args, **kwargs)

    monkeypatch.setattr(type(checkpoint), "compare_and_set_bound", measured_cas)
    monkeypatch.setattr(loader, "measure_and_log_lag", lambda *args: SimpleNamespace(mirror_lag_seconds=None))
    return storage, table, checkpoint, end, Cursor(clock, failure), records, trace


def run(storage, table, cursor):
    return loader.run_once(storage=storage, tables=(table,), raw_prefix_root="raw",
                           database="D", schema="S", streaming_client_factory=None,
                           cursor=cursor, ensure_schema=False, history_mode="sql")


def stages(records):
    return [r for r in records if r["record_type"] == "stage"]


def test_bound_loader_attributes_preflights_sql_and_cas_without_payload(tmp_path, monkeypatch):
    storage, table, checkpoint, end, cursor, records, _ = prepare(tmp_path, monkeypatch)
    run(storage, table, cursor)
    measured = stages(records)
    assert [r["stage"] for r in measured] == [
        "loader_outer_preflight", "loader_preflight", "history_execute", "mirror_execute", "checkpoint_cas"]
    assert [r["duration_ms"] for r in measured] == pytest.approx([500, 500, 2000, 3000, 200])
    assert measured[2]["query_id_after"] == "00000000-0000-0000-0000-aaaaaaaaaa01"
    assert measured[3]["query_id_after"] == "00000000-0000-0000-0000-aaaaaaaaaa02"
    assert len({r["trace_id"] for r in measured}) == 1
    assert checkpoint.load_bound() == end
    store = storage.object_store("raw/cntr/journal")
    raw = json.loads(store.get(store.list_receipt_keys(10)[0]))["raw"]
    manifest = json.loads(store.get(raw["manifest_key"]))
    encoded = json.dumps(records)
    for digest in (manifest["batch_id"], manifest["payload_sha256"],
                   hashlib.sha256(table.table_id.encode()).hexdigest()):
        assert digest in encoded
    assert all(term not in encoded for term in ("CNTR", "table-1", "MERGE INTO", "source", "password="))
    assert all(r["clock_error_bound_ms"] is None for r in measured)
    assert records[-1]["record_type"] == "summary"
    assert records[-1]["status"] == "completed"


def test_bound_history_failure_keeps_exception_and_checkpoint(tmp_path, monkeypatch):
    failure = RuntimeError("password=never-publish-this-value")
    storage, table, checkpoint, _, cursor, records, _ = prepare(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(RuntimeError) as caught:
        run(storage, table, cursor)
    assert caught.value is failure
    assert checkpoint.load_bound() == BOOTSTRAP
    assert [r["stage"] for r in stages(records)] == [
        "loader_outer_preflight", "loader_preflight", "history_execute"]
    assert stages(records)[-1]["status"] == "failed"
    assert records[-1]["status"] == "failed"
    assert "never-publish" not in json.dumps(records)
    assert len(cursor.statements) == 1


def test_outer_preflight_failure_records_no_sql_or_checkpoint_effect(tmp_path, monkeypatch):
    storage, table, checkpoint, _, cursor, records, _ = prepare(tmp_path, monkeypatch)
    failure = ValueError("private evidence mismatch")

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(loader, "_bound_preflight", refuse)
    with pytest.raises(ValueError) as caught:
        run(storage, table, cursor)
    assert caught.value is failure
    assert not cursor.statements
    assert checkpoint.load_bound() == BOOTSTRAP
    assert [(r["stage"], r["status"]) for r in stages(records)] == [("loader_outer_preflight", "failed")]
    assert records[-1]["status"] == "failed"


def test_checkpoint_race_still_occurs_after_both_sql_statements(tmp_path, monkeypatch):
    storage, table, checkpoint, _, cursor, records, _ = prepare(tmp_path, monkeypatch)
    failure = RuntimeError("concurrent checkpoint owner")

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(type(checkpoint), "compare_and_set_bound", refuse)
    with pytest.raises(RuntimeError) as caught:
        run(storage, table, cursor)
    assert caught.value is failure
    assert checkpoint.load_bound() == BOOTSTRAP
    assert len(cursor.statements) == 2
    assert [(r["stage"], r["status"]) for r in stages(records)][-2:] == [
        ("mirror_execute", "completed"), ("checkpoint_cas", "failed")]


def test_trace_sink_failure_cannot_change_delivery(tmp_path, monkeypatch):
    def broken_sink(record):
        raise RuntimeError("log transport down")

    storage, table, checkpoint, end, cursor, _, _ = prepare(tmp_path, monkeypatch, emit=broken_sink)
    run(storage, table, cursor)
    assert checkpoint.load_bound() == end
    assert len(cursor.statements) == 2


def test_empty_snapshot_clear_and_seed_are_separate_from_cdc(tmp_path, monkeypatch):
    storage, table, checkpoint, end, cursor, records, _ = prepare(tmp_path, monkeypatch, initialized=False)
    run(storage, table, cursor)
    assert [r["stage"] for r in stages(records)] == [
        "loader_outer_preflight", "loader_preflight", "snapshot_clear", "checkpoint_cas",
        "history_execute", "mirror_execute", "checkpoint_cas"]
    assert checkpoint.load_bound() == end
    assert cursor.statements[0].startswith("DELETE FROM")
    assert records[-1]["status"] == "completed"


def test_trace_disabled_preserves_same_dml_and_checkpoint(tmp_path, monkeypatch, caplog):
    storage, table, checkpoint, end, cursor, records, _ = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(loader, "stage_trace_from_environment", lambda *args, **kwargs: None)
    run(storage, table, cursor)
    assert not records and "stage_timing" not in caplog.text
    assert checkpoint.load_bound() == end
    assert len(cursor.statements) == 2


def test_idle_bound_cycle_has_no_synthetic_sql_execution(tmp_path, monkeypatch):
    storage, table, checkpoint, end, cursor, records, _ = prepare(tmp_path, monkeypatch)
    checkpoint.compare_and_set_bound(BOOTSTRAP, end)
    run(storage, table, cursor)
    assert not cursor.statements
    assert [r["stage"] for r in stages(records)] == ["loader_outer_preflight", "loader_preflight"]
    assert checkpoint.load_bound() == end
    assert records[-1]["status"] == "completed"


def test_corrupt_binding_still_refuses_before_dml(tmp_path, monkeypatch):
    storage, table, checkpoint, _, cursor, records, _ = prepare(tmp_path, monkeypatch)
    foreign = replace(table, capture_binding=replace(table.capture_binding, source_system="foreign"))
    with pytest.raises(loader.DestinationLoaderError):
        run(storage, foreign, cursor)
    assert not cursor.statements
    assert checkpoint.load_bound() == BOOTSTRAP
    assert records[-1]["status"] == "failed"
