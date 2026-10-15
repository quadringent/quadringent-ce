from io import BytesIO
from decimal import Decimal
from dataclasses import replace
import hashlib
import json

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.qualification.verification_contract import Column, ReceiverWindow, VerificationScope
from quadringent.qualification.verification_stream import FrameReader, write_stream
from quadringent.qualification.verification_ledger import DiskReconciler, VerificationError


START = "2026-01-01T00:00:00Z"
END = "2026-01-01T00:00:01Z"


def scope(*, rows=2, columns=None, primary_key=("ID",)):
    identity = ReceiverIdentity("TESTLIB", "JRN", "TESTLIB", "RCV1", "offline-test-v1", "generation-one")
    binding = CaptureBinding("scope-one", "epoch-one", "ibmi", "JRN",
                             "s3://synthetic-raw/receiver-authority-v1",
                             BoundCheckpoint("scope-one", "epoch-one", identity, 20))
    return VerificationScope(
        pipeline_id="pipeline-one", table_id="table-one", copy_run_id="run-one",
        source_library="TESTLIB", source_table="ITEMS", destination_database="DEST",
        destination_schema="TEST", history_table="ITEMS_HISTORY", mirror_table="ITEMS_MIRROR",
        columns=columns or (Column("ID", "int"), Column("LABEL", "text"), Column("AMOUNT", "decimal")),
        primary_key=primary_key, capture_binding=binding, copy_boundary=binding.bootstrap,
        checkpoint=binding.checkpoint(identity, 24),
        receiver_windows=(ReceiverWindow(identity, 21, 24),),
        expected_snapshot_rows=rows, producer_revision="b" * 40, producer_version="0.4.5",
    )


def event(s, row, receiver, sequence, operation):
    journal = "SNAPSHOT:TESTLIB.ITEMS" if receiver.startswith("SNAPSHOT:") else s.capture_binding.journal
    event_id = hashlib.sha256(f"ibmi|{journal}|{receiver}|{sequence}".encode()).hexdigest()
    return {**row, "EVENT_ID": event_id, "OPERATION": operation,
            "JOURNAL_RECEIVER": receiver, "JOURNAL_SEQUENCE": str(sequence)}


def layers(s):
    first = {"ID": "1", "LABEL": "first  ", "AMOUNT": "12345678901234567890123456789012345678.00"}
    second = {"ID": 2, "LABEL": None, "AMOUNT": None}
    updated = {**first, "LABEL": ""}
    inserted = {"ID": 3, "LABEL": "third", "AMOUNT": "-0.0000"}
    snapshot = [event(s, first, "SNAPSHOT:run-one", 1, "c"),
                event(s, second, "SNAPSHOT:run-one", 2, "c")]
    journal = [event(s, first, "RCV1", 21, "u_before"),
               event(s, updated, "RCV1", 22, "u_after"),
               event(s, inserted, "RCV1", 23, "c"),
               event(s, second, "RCV1", 24, "d")]
    return {"source": [updated, inserted], "snapshot": snapshot, "journal": journal,
            "positions": [{"JOURNAL_RECEIVER": x["JOURNAL_RECEIVER"],
                           "JOURNAL_SEQUENCE": x["JOURNAL_SEQUENCE"]} for x in journal],
            "raw": snapshot + journal, "history": snapshot + journal,
            "mirror": [{**updated, "ID": Decimal(1), "AMOUNT": Decimal(first["AMOUNT"])},
                       {**inserted, "AMOUNT": Decimal(0)}]}


def frame(s, dataset, rows):
    data = BytesIO()
    write_stream(data, rows, scope_sha256=s.sha256, dataset=dataset, started_at=START, completed_at=END)
    data.seek(0)
    return FrameReader(data, scope_sha256=s.sha256, dataset=dataset)


def run_all(tmp_path, s, data):
    with DiskReconciler(tmp_path / "verification", s) as engine:
        for dataset, rows in data.items():
            engine.ingest(dataset, frame(s, dataset, rows))
        return engine.finish()


def test_complete_independent_layers_preserve_all_fields_and_distinct_populations(tmp_path):
    s = scope()
    report = run_all(tmp_path, s, layers(s))
    assert report["state"] == "verified"
    assert report["scope_sha256"] == s.sha256
    assert report["datasets"]["source"]["row_count"] == 2
    assert report["datasets"]["history"]["row_count"] == 6
    assert report["datasets"]["journal"]["row_count"] == 4
    assert report["ignored_fields"] == 0
    assert all(x["equal"] for x in report["comparisons"].values())
    assert "native_cdc_qualified" not in report
    assert "12345678901234567890" not in json.dumps(report)
    assert "first  " not in json.dumps(report)
    assert (tmp_path / "verification").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "verification" / "ledger.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("dataset", ["source", "snapshot", "journal", "positions", "raw", "history", "mirror"])
def test_no_dataset_may_be_omitted(tmp_path, dataset):
    s = scope()
    data = layers(s)
    data.pop(dataset)
    report = run_all(tmp_path, s, data)
    assert report["state"] == "incomplete"
    assert report["datasets"][dataset] is None
    assert report["comparisons"] is None


@pytest.mark.parametrize("dataset", ["source", "snapshot", "journal", "positions", "history", "mirror"])
def test_duplicate_logical_identity_never_passes(tmp_path, dataset):
    s = scope()
    data = layers(s)
    data[dataset].append(data[dataset][0])
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"
    assert report["datasets"][dataset]["duplicate_count"] == 1


def test_exact_raw_replay_is_reported_separately_from_logical_duplicates(tmp_path):
    s = scope()
    data = layers(s)
    data["raw"].append(data["raw"][0])
    report = run_all(tmp_path, s, data)
    assert report["state"] == "verified"
    assert report["datasets"]["raw"]["exact_replay_count"] == 1
    assert report["datasets"]["raw"]["conflicting_identity_count"] == 0


def test_conflicting_raw_identity_is_not_silently_deduplicated(tmp_path):
    s = scope()
    data = layers(s)
    data["raw"].append({**data["raw"][0], "LABEL": "changed"})
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"
    assert report["datasets"]["raw"]["conflicting_identity_count"] == 1


@pytest.mark.parametrize("dataset", ["raw", "history", "mirror", "source", "positions"])
def test_missing_and_extra_rows_are_detected(tmp_path, dataset):
    s = scope()
    data = layers(s)
    data[dataset].pop()
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"


@pytest.mark.parametrize("column,value", [("AMOUNT", "12345678901234567890123456789012345679"),
                                          ("LABEL", "first"), ("LABEL", None)])
def test_all_business_values_are_compared_without_rounding_or_trimming(tmp_path, column, value):
    s = scope()
    data = layers(s)
    data["history"][0] = {**data["history"][0], column: value}
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"
    assert report["comparisons"]["history"]["different_count"] == 1


@pytest.mark.parametrize("mutation", [
    lambda row: {**row, "EXTRA": "must not be ignored"},
    lambda row: {k: v for k, v in row.items() if k != "AMOUNT"},
    lambda row: {**row, "ID": None},
    lambda row: {**row, "ID": True},
    lambda row: {**row, "AMOUNT": "NaN"},
])
def test_malformed_or_inexact_rows_make_the_stream_incomplete(tmp_path, mutation):
    s = scope()
    data = layers(s)
    with DiskReconciler(tmp_path / "verification", s) as engine:
        with pytest.raises(VerificationError):
            engine.ingest("source", frame(s, "source", [mutation(data["source"][0])]))
        report = engine.finish()
    assert report["state"] == "incomplete"
    assert report["datasets"]["source"] is None


def test_truncated_stream_rolls_back_and_cannot_be_replaced_silently(tmp_path):
    s = scope()
    # Build the damaged original independently of reader implementation details.
    raw = BytesIO()
    write_stream(raw, layers(s)["source"], scope_sha256=s.sha256, dataset="source",
                 started_at=START, completed_at=END)
    damaged = FrameReader(BytesIO(raw.getvalue().rsplit(b"\n", 2)[0] + b"\n"),
                          scope_sha256=s.sha256, dataset="source")
    with DiskReconciler(tmp_path / "verification", s) as engine:
        with pytest.raises(VerificationError):
            engine.ingest("source", damaged)
        with pytest.raises(VerificationError):
            engine.ingest("source", frame(s, "source", layers(s)["source"]))
        assert engine.finish()["state"] == "incomplete"


def test_reader_prepared_for_other_scope_or_dataset_is_refused(tmp_path):
    s = scope()
    other = scope(rows=3)
    with DiskReconciler(tmp_path / "verification", s) as engine:
        with pytest.raises(VerificationError):
            engine.ingest("source", frame(other, "source", []))
        with pytest.raises(VerificationError):
            engine.ingest("mirror", frame(s, "source", []))


def test_original_source_positions_are_required_even_when_other_layers_agree(tmp_path):
    s = scope()
    data = layers(s)
    for dataset in ("journal", "raw", "history"):
        data[dataset] = [r for r in data[dataset] if r.get("JOURNAL_SEQUENCE") != "22"]
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"
    assert report["comparisons"]["journal_positions"]["missing_count"] == 1


def test_source_is_checked_against_independently_reconstructed_mirror(tmp_path):
    s = scope()
    data = layers(s)
    for dataset in ("source", "mirror"):
        data[dataset][0] = {**data[dataset][0], "LABEL": "same wrong destination"}
    report = run_all(tmp_path, s, data)
    assert report["state"] == "divergent"
    assert report["comparisons"]["source"]["different_count"] == 1
    assert report["comparisons"]["mirror"]["equal"] is True


def test_snapshot_count_is_bound_to_original_copy_receipt(tmp_path):
    s = scope(rows=3)
    report = run_all(tmp_path, s, layers(s))
    assert report["state"] == "divergent"
    assert report["checks"]["snapshot_rows_complete"] is False


def test_existing_or_symlink_output_is_refused(tmp_path):
    s = scope()
    target = tmp_path / "verification"
    target.mkdir()
    with pytest.raises(VerificationError):
        DiskReconciler(target, s)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(VerificationError):
        DiskReconciler(link / "new", s)


def test_empty_but_complete_layers_are_measured_zero(tmp_path):
    s = scope(rows=0)
    report = run_all(tmp_path, s, {dataset: [] for dataset in
                                 ("source", "snapshot", "journal", "positions", "raw", "history", "mirror")})
    assert report["state"] == "verified"
    assert all(d["row_count"] == 0 for d in report["datasets"].values())


def test_composite_primary_key_keeps_equal_first_key_values_distinct(tmp_path):
    s = scope(primary_key=("ID", "LABEL"))
    rows = [{"ID": 1, "LABEL": "a", "AMOUNT": None}, {"ID": 1, "LABEL": "b", "AMOUNT": "1.2300"}]
    snapshot = [
        event(s, row, "SNAPSHOT:run-one", i, "c") for i, row in enumerate(rows, 1)]
    report = run_all(tmp_path, s, {"source": rows, "snapshot": snapshot, "journal": [], "positions": [],
                                   "raw": snapshot, "history": snapshot, "mirror": list(reversed(rows))})
    assert report["state"] == "verified"


def test_native_sequences_do_not_pass_through_sqlite_signed_integer(tmp_path):
    s = scope()
    origin = 10**20
    identity = s.copy_boundary.receiver_identity
    s = replace(s, copy_boundary=s.capture_binding.checkpoint(identity, origin),
                checkpoint=s.capture_binding.checkpoint(identity, origin + 4),
                receiver_windows=(ReceiverWindow(identity, origin + 1, origin + 4),))
    data = layers(scope())
    for dataset in ("journal", "raw", "history"):
        data[dataset] = [event(s, {c.name: row[c.name] for c in s.columns}, row["JOURNAL_RECEIVER"],
                               origin + int(row["JOURNAL_SEQUENCE"]) - 20, row["OPERATION"])
                         if row["JOURNAL_RECEIVER"] == "RCV1" else row for row in data[dataset]]
    data["positions"] = [{"JOURNAL_RECEIVER": row["JOURNAL_RECEIVER"],
                          "JOURNAL_SEQUENCE": row["JOURNAL_SEQUENCE"]} for row in data["journal"]]
    assert run_all(tmp_path, s, data)["state"] == "verified"


def test_database_budget_failure_cannot_return_a_partial_success(tmp_path):
    s = scope()
    rows = ({"ID": i, "LABEL": "x" * 4096, "AMOUNT": "0"} for i in range(500))
    with DiskReconciler(tmp_path / "verification", s, max_database_bytes=262144) as engine:
        with pytest.raises(VerificationError, match="invalid_or_incomplete_stream"):
            engine.ingest("source", frame(s, "source", rows))
        assert engine.finish()["state"] == "incomplete"


def test_exception_after_comparison_cannot_leave_verified_state(tmp_path, monkeypatch):
    import quadringent.qualification.verification_ledger as module
    s = scope()
    with DiskReconciler(tmp_path / "verification", s) as engine:
        for dataset, rows in layers(s).items():
            engine.ingest(dataset, frame(s, dataset, rows))
        monkeypatch.setattr(module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
        report = engine.finish()
    assert report["state"] == "incomplete"
    assert report["comparisons"] is None


def test_primary_key_update_reconstructs_removed_and_added_row_images(tmp_path):
    s = scope()
    initial = {"ID": 1, "LABEL": "before", "AMOUNT": "2"}
    final = {**initial, "ID": 3, "LABEL": "after"}
    s = replace(s, expected_snapshot_rows=1,
                checkpoint=s.capture_binding.checkpoint(s.checkpoint.receiver_identity, 22),
                receiver_windows=(ReceiverWindow(s.checkpoint.receiver_identity, 21, 22),))
    snap = [event(s, initial, "SNAPSHOT:run-one", 1, "c")]
    journal = [event(s, initial, "RCV1", 21, "u_before"), event(s, final, "RCV1", 22, "u_after")]
    report = run_all(tmp_path, s, {"source": [final], "snapshot": snap, "journal": journal,
                                  "positions": [{"JOURNAL_RECEIVER": "RCV1", "JOURNAL_SEQUENCE": str(i)}
                                                for i in (21, 22)],
                                  "raw": snap + journal, "history": snap + journal, "mirror": [final]})
    assert report["state"] == "verified"


@pytest.mark.parametrize("value", ["1E+1000000000", "1E-1000000000"])
def test_short_decimal_cannot_expand_before_resource_budgets(tmp_path, monkeypatch, value):
    import quadringent.qualification.verification_ledger as module
    original = module.normalize
    reached = []
    def guarded(value, kind):
        if kind == "decimal" and abs(Decimal(str(value)).as_tuple().exponent) > 10000:
            reached.append(True)
            raise RuntimeError("unsafe decimal expansion intercepted")
        return original(value, kind)
    monkeypatch.setattr(module, "normalize", guarded)
    s = scope()
    with DiskReconciler(tmp_path / "verification", s) as engine:
        with pytest.raises(VerificationError):
            engine.ingest("source", frame(s, "source", [{"ID": 1, "LABEL": "", "AMOUNT": value}]))
    assert reached == []


def test_deadline_during_final_sync_cannot_return_verified(tmp_path, monkeypatch):
    import quadringent.qualification.verification_ledger as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    s = scope()
    with DiskReconciler(tmp_path / "verification", s, max_seconds=1) as engine:
        for dataset, rows in layers(s).items():
            engine.ingest(dataset, frame(s, dataset, rows))
        monkeypatch.setattr(module.os, "fsync", lambda _: clock.__setitem__(0, 2.0))
        assert engine.finish()["state"] == "incomplete"


def test_zero_with_huge_exponent_is_normalized_without_expansion(tmp_path):
    s = scope()
    data = layers(s)
    data["mirror"][1]["AMOUNT"] = "0E+1000000000"
    assert run_all(tmp_path, s, data)["state"] == "verified"




