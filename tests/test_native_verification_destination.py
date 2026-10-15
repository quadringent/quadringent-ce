from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import hashlib

import pytest

from quadringent.qualification.verification_contract import Column
from quadringent.qualification.verification_stream import FrameReader, StreamError, StreamLimits
from quadringent.qualification.verification_ledger import DiskReconciler
from quadringent.qualification import verification_destination as destination
from test_native_verification_ledger import frame, layers, scope


START = "2026-01-01T00:00:00.123456Z"
TECH = ("EVENT_ID", "OPERATION", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE")


def description(names, codes=None):
    codes = codes or [0 if name in {"ID", "AMOUNT", "JOURNAL_SEQUENCE"} else 2 for name in names]
    return [(name, code, None, None, 38 if code == 0 else None,
             18 if name == "AMOUNT" else 0 if code == 0 else 9 if code == 8 else None, True)
            for name, code in zip(names, codes, strict=True)]


class Cursor:
    def __init__(self, metadata, rows=(), *, rowcount=None, fault=None, on_fetch=None):
        self.description = metadata
        self.rows = list(rows)
        self.rowcount = len(self.rows) if rowcount is None else rowcount
        self.fault = fault
        self.on_fetch = on_fetch
        self.closed = False
        self.calls = []
        self.sizes = []

    def execute(self, sql, *, timeout):
        self.calls.append((sql, timeout))
        if self.fault == "execute":
            raise RuntimeError("SYNTHETIC_SECRET SQL")
        return self

    def fetchmany(self, size):
        assert not self.closed
        self.sizes.append(size)
        if self.on_fetch:
            self.on_fetch()
        if self.fault == "fetch":
            raise RuntimeError("SYNTHETIC_SECRET row")
        batch, self.rows = self.rows[:size], self.rows[size:]
        if self.fault == "oversized":
            return batch * (size + 1)
        return batch

    def close(self):
        self.closed = True
        if self.fault == "close":
            raise RuntimeError("SYNTHETIC_SECRET close")


class Connection:
    network_timeout = 1

    def __init__(self, probe, data):
        self.cursors = [probe, data]
        self.opened = []

    def cursor(self):
        cursor = self.cursors[len(self.opened)]
        self.opened.append(cursor)
        return cursor


def fixture(*, dataset="mirror", rows=None, custom_scope=None, codes=None):
    s = custom_scope or scope()
    names = [column.name for column in s.columns] + (list(TECH) if dataset == "history" else [])
    metadata = description(names, codes)
    rows = [(1, "Café 日本語  ", Decimal("12345678901234567890.123456789012345678"))] if rows is None else rows
    probe = Cursor(metadata)
    projected = description(names, [2 if code == 8 else code for code in
                                  (codes or [entry[1] for entry in metadata])])
    projected += description(["_QDT_OVERSIZE"], [13])
    data = Cursor(projected, [(*row, False) for row in rows])
    return s, Connection(probe, data)


def run(connection, s=None, *, output=None, dataset="mirror", **kwargs):
    return destination.export_destination(connection, s or scope(), output if output is not None else BytesIO(),
                                          dataset=dataset, **kwargs)


def assert_incomplete(output):
    output.seek(0)
    reader = FrameReader(output, scope_sha256=scope().sha256, dataset="mirror")
    with pytest.raises(StreamError):
        list(reader)
    assert reader.summary is None


def test_exact_mirror_frames_and_bounded_selects(monkeypatch):
    monkeypatch.setattr(destination, "_utc_now", lambda: START)
    s, connection = fixture(rows=[(1, "  ", Decimal("-0.00")), (2, "", None)])
    output = BytesIO()
    summary = run(connection, s, output=output, fetch_size=1)
    output.seek(0)
    reader = FrameReader(output, scope_sha256=s.sha256, dataset="mirror")
    assert list(reader) == [{"ID": 1, "LABEL": "  ", "AMOUNT": "-0.00"},
                            {"ID": 2, "LABEL": "", "AMOUNT": None}]
    assert reader.summary == summary
    assert summary.sha256 == hashlib.sha256(output.getvalue()).hexdigest()
    assert summary.started_at == START
    assert all(cursor.closed for cursor in connection.opened)
    probe, data = connection.opened
    assert probe.calls[0][0] == 'SELECT "ID","LABEL","AMOUNT" FROM "DEST"."TEST"."ITEMS_MIRROR" WHERE 1=0'
    assert 'CASE WHEN "_QDT_TEXT_BYTES"<=1048576 THEN "LABEL" ELSE NULL END AS "LABEL"' in data.calls[0][0]
    assert 'COALESCE(OCTET_LENGTH("LABEL"),0) AS "_QDT_TEXT_BYTES"' in data.calls[0][0]
    assert '"_QDT_TEXT_BYTES">1048576 AS "_QDT_OVERSIZE"' in data.calls[0][0]
    assert 'FROM "DEST"."TEST"."ITEMS_MIRROR"' in data.calls[0][0]
    assert data.calls[0][0].endswith('ORDER BY "ID"')
    assert all(type(timeout) is int and 0 < timeout <= 1800
               for cursor in connection.opened for _, timeout in cursor.calls)
    assert data.sizes == [1, 1, 1]
    assert all(sql.startswith("SELECT ") and "COUNT(" not in sql
               for cursor in connection.opened for sql, _ in cursor.calls)


def test_history_exports_all_business_and_technical_fields():
    row = (1, None, Decimal("1.00"), "a" * 64, "u_after", "RCV1", Decimal(21))
    s, connection = fixture(dataset="history", rows=[row])
    output = BytesIO()
    summary = run(connection, s, output=output, dataset="history")
    output.seek(0)
    reader = FrameReader(output, scope_sha256=s.sha256, dataset="history")
    assert list(reader) == [{"ID": 1, "LABEL": None, "AMOUNT": "1.00", "EVENT_ID": "a" * 64,
                            "OPERATION": "u_after", "JOURNAL_RECEIVER": "RCV1", "JOURNAL_SEQUENCE": "21"}]
    assert summary.row_count == 1
    assert connection.opened[-1].calls[0][0].endswith('ORDER BY "EVENT_ID"')


def test_timestamp_projection_preserves_nine_digits_and_date():
    s = scope(columns=(Column("ID", "int"), Column("STAMP", "timestamp"), Column("DAY", "date")))
    s, connection = fixture(custom_scope=s, codes=[0, 8, 3],
                            rows=[(1, "2026-01-02T03:04:05.123456789", date(2026, 1, 2))])
    output = BytesIO()
    run(connection, s, output=output)
    sql = connection.opened[-1].calls[0][0]
    assert 'TO_CHAR("STAMP",\'YYYY-MM-DD"T"HH24:MI:SS.FF9\') AS "STAMP"' in sql
    output.seek(0)
    assert list(FrameReader(output, scope_sha256=s.sha256, dataset="mirror"))[0]["STAMP"].endswith("123456789")


@pytest.mark.parametrize("value", [1.25, float("nan"), float("inf"), True, {"nested": 1.0}, [1.0]])
def test_inexact_numeric_values_refused(value):
    _, connection = fixture(rows=[(1, "ok", value)])
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError, match="^destination_export_failed$"):
        run(connection, output=output)
    assert_incomplete(output)
    assert all(cursor.closed for cursor in connection.opened)


@pytest.mark.parametrize("fault", ["execute", "fetch", "close", "oversized"])
def test_cursor_failures_and_close_failure_do_not_seal_frames(fault):
    _, connection = fixture()
    connection.cursors[-1].fault = fault
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError) as error:
        run(connection, output=output)
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert error.value.__cause__ is None
    assert_incomplete(output)
    assert all(cursor.closed for cursor in connection.opened)


@pytest.mark.parametrize("rowcount", [0, 2, -1, None, True])
def test_truncated_or_unknown_query_population_is_refused(rowcount):
    _, connection = fixture()
    connection.cursors[-1].rowcount = rowcount
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert_incomplete(output)


@pytest.mark.parametrize("metadata", [None, [], description(["WRONG", "LABEL", "AMOUNT"]),
                                      description(["ID", "LABEL", "AMOUNT"], [1, 2, 0]),
                                      description(["ID", "LABEL", "AMOUNT"], [0, 5, 0])])
def test_bad_original_metadata_refused_before_output(metadata):
    _, connection = fixture()
    connection.cursors[0].description = metadata
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert not output.getvalue()
    assert len(connection.opened) == 1 and connection.opened[0].closed


@pytest.mark.parametrize("code", [4, 6, 7])
def test_ambiguous_and_zoned_timestamps_refused(code):
    s = scope(columns=(Column("ID", "int"), Column("STAMP", "timestamp")))
    _, connection = fixture(custom_scope=s, rows=[], codes=[0, code])
    with pytest.raises(destination.DestinationExportError):
        run(connection, s)


@pytest.mark.parametrize("stamp", [datetime(2026, 1, 1), "2026-01-01T00:00:00.123456",
                                  "2026-02-30T00:00:00.123456789", "2026-01-01T24:00:00.000000000"])
def test_lossy_or_invalid_projected_timestamp_refused(stamp):
    s = scope(columns=(Column("ID", "int"), Column("STAMP", "timestamp")))
    _, connection = fixture(custom_scope=s, codes=[0, 8], rows=[(1, stamp)])
    with pytest.raises(destination.DestinationExportError):
        run(connection, s)


@pytest.mark.parametrize("limits", [StreamLimits(max_rows=1), StreamLimits(max_bytes=200),
                                    StreamLimits(max_line_bytes=250)])
def test_stream_budgets_refuse_incomplete_output(limits):
    _, connection = fixture(rows=[(1, "x" * 400, Decimal(1)), (2, "y", Decimal(2))])
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output, limits=limits)
    assert_incomplete(output)


@pytest.mark.parametrize("timeout", [None, True, 0, -1, 1801, 1.5])
def test_network_bound_required_before_any_query(timeout):
    _, connection = fixture()
    connection.network_timeout = timeout
    with pytest.raises(destination.DestinationExportError):
        run(connection)
    assert not connection.opened


def test_global_deadline_covers_fetch_and_closes_cursor(monkeypatch):
    ticks = [0.0]
    monkeypatch.setattr(destination.time, "monotonic", lambda: ticks[0])
    _, connection = fixture()
    connection.cursors[-1].on_fetch = lambda: ticks.__setitem__(0, 1801.0)
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert_incomplete(output)
    assert all(cursor.closed for cursor in connection.opened)


def test_empty_population_and_composite_key_ordering():
    s = replace(scope(), primary_key=("LABEL", "ID"))
    _, connection = fixture(custom_scope=s, rows=[])
    assert run(connection, s).row_count == 0
    assert connection.opened[-1].calls[0][0].endswith('ORDER BY "LABEL","ID"')


@pytest.mark.parametrize("dataset", ["source", "raw", "HISTORY", None])
def test_only_two_destination_datasets_are_admitted(dataset):
    _, connection = fixture()
    with pytest.raises(destination.DestinationExportError):
        run(connection, dataset=dataset)
    assert not connection.opened


@pytest.mark.parametrize("size", [0, -1, True, 10001, 1.5])
def test_fetch_size_hard_cap(size):
    _, connection = fixture()
    with pytest.raises(destination.DestinationExportError):
        run(connection, fetch_size=size)
    assert not connection.opened


def test_flush_failure_returns_no_summary_even_when_footer_was_written():
    class BrokenFlush(BytesIO):
        def flush(self):
            raise OSError("SYNTHETIC_SECRET")

    _, connection = fixture()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=BrokenFlush())


@pytest.mark.parametrize("flag", [True, None, 0, 1, "false"])
def test_server_oversize_or_invalid_indicator_is_never_admitted(flag):
    _, connection = fixture()
    connection.cursors[-1].rows = [(1, None, Decimal(1), flag)]
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert b'"kind":"row"' not in output.getvalue()
    assert_incomplete(output)


def test_server_guard_covers_all_history_text_without_silent_truncation():
    _, connection = fixture(dataset="history", rows=[])
    run(connection, dataset="history", limits=StreamLimits(max_line_bytes=512))
    sql = connection.opened[-1].calls[0][0]
    for name in ("LABEL", "EVENT_ID", "OPERATION", "JOURNAL_RECEIVER"):
        assert f'COALESCE(OCTET_LENGTH("{name}"),0)' in sql
        assert f'THEN "{name}" ELSE NULL END AS "{name}"' in sql
    assert "SUBSTR" not in sql and "LEFT(" not in sql and "<=512" in sql


def test_text_sequence_is_guarded_and_preserved():
    names = ["ID", "LABEL", "AMOUNT", *TECH]
    codes = [0, 2, 0, 2, 2, 2, 2]
    row = (1, "", Decimal(1), "a" * 64, "c", "SNAPSHOT:run-one", "1")
    s, connection = fixture(dataset="history", codes=codes, rows=[row])
    output = BytesIO()
    run(connection, s, output=output, dataset="history")
    assert connection.cursors[0].description == description(names, codes)
    assert 'COALESCE(OCTET_LENGTH("JOURNAL_SEQUENCE"),0)' in connection.cursors[-1].calls[0][0]
    output.seek(0)
    assert list(FrameReader(output, scope_sha256=s.sha256, dataset="history"))[0]["JOURNAL_SEQUENCE"] == "1"


@pytest.mark.parametrize("mutation", ["name", "code", "precision", "scale", "flag"])
def test_projected_metadata_drift_is_refused_before_header(mutation):
    _, connection = fixture()
    data = connection.cursors[-1]
    index, slot, value = {"name": (0, 0, "OTHER"), "code": (0, 1, 1),
                          "precision": (2, 4, 20), "scale": (2, 5, 4), "flag": (3, 1, 2)}[mutation]
    entry = list(data.description[index])
    entry[slot] = value
    data.description[index] = tuple(entry)
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert not output.getvalue()
    assert all(cursor.closed for cursor in connection.opened)


@pytest.mark.parametrize("phase", ["probe_execute", "probe_close", "fetch"])
def test_network_bound_is_rechecked_and_never_silently_increased(phase):
    _, connection = fixture()
    probe, data = connection.cursors
    if phase == "probe_execute":
        original = probe.execute

        def execute(*args, **kwargs):
            result = original(*args, **kwargs)
            connection.network_timeout = None
            return result

        probe.execute = execute
    elif phase == "probe_close":
        original = probe.close

        def close():
            original()
            connection.network_timeout = 1801

        probe.close = close
    else:
        data.on_fetch = lambda: setattr(connection, "network_timeout", 1801)
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert connection.network_timeout in (None, 1801)
    assert_incomplete(output)
    assert all(cursor.closed for cursor in connection.opened)


def test_fetch_size_also_bounds_application_batch_text_allowance():
    _, connection = fixture(rows=[])
    run(connection, fetch_size=10000, limits=StreamLimits(max_line_bytes=8 * 1024**2))
    assert connection.cursors[-1].sizes == [2]


def test_internal_sql_aliases_do_not_shadow_business_columns():
    s = scope(columns=(Column("ID", "int"), Column("_QDT_TEXT_BYTES", "text"),
                       Column("_QDT_OVERSIZE", "text")))
    _, connection = fixture(custom_scope=s, codes=[0, 2, 2], rows=[(1, "a", "b")])
    connection.cursors[-1].description[-1] = description(["_QDT_OVERSIZE_1"], [13])[0]
    output = BytesIO()
    run(connection, s, output=output)
    sql = connection.cursors[-1].calls[0][0]
    assert 'AS "_QDT_TEXT_BYTES_1"' in sql and 'AS "_QDT_OVERSIZE_1"' in sql
    output.seek(0)
    assert list(FrameReader(output, scope_sha256=s.sha256, dataset="mirror"))[0] == {
        "ID": 1, "_QDT_TEXT_BYTES": "a", "_QDT_OVERSIZE": "b"}


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("1E+1000000"),
                                  Decimal("1E-1000000")])
def test_invalid_or_unbounded_decimal_cannot_expand_in_memory(value):
    _, connection = fixture(rows=[(1, "", value)])
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert_incomplete(output)


def test_rows_are_consumed_once_and_close_precedes_footer():
    _, connection = fixture()

    class CheckClose(BytesIO):
        def write(self, value):
            if b'"kind":"end"' in value:
                assert all(cursor.closed for cursor in connection.opened)
            return super().write(value)

    assert run(connection, output=CheckClose(), fetch_size=1).row_count == 1
    assert connection.cursors[-1].sizes == [1, 1]


def test_probe_close_failure_never_opens_data_query():
    _, connection = fixture()
    connection.cursors[0].fault = "close"
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert len(connection.opened) == 1 and not output.getvalue()


def test_boolean_rowcount_drift_at_eof_is_not_an_integer_population():
    _, connection = fixture()
    data = connection.cursors[-1]
    data.on_fetch = lambda: setattr(data, "rowcount", True)
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert_incomplete(output)


@pytest.mark.parametrize("value", ["\ud800", "\udc00", "a\ud800z"])
def test_invalid_unicode_from_driver_is_refused_without_replacement(value):
    _, connection = fixture(rows=[(1, value, None)])
    output = BytesIO()
    with pytest.raises(destination.DestinationExportError):
        run(connection, output=output)
    assert_incomplete(output)


@pytest.mark.parametrize("alter_last_mirror", [False, True])
def test_destination_frames_join_real_ledger_and_last_row_changes_cannot_verify(tmp_path, alter_last_mirror):
    s = scope()
    data = layers(s)
    for records in data.values():
        for record in records:
            if str(record.get("AMOUNT")) == "12345678901234567890123456789012345678.00":
                record["AMOUNT"] = "12345678901234567890.123456789012345678"
    with DiskReconciler(tmp_path / "ledger", s) as engine:
        for dataset, records in data.items():
            if dataset not in {"history", "mirror"}:
                engine.ingest(dataset, frame(s, dataset, records))
                continue
            values = []
            for record in records:
                label = "changed last row" if alter_last_mirror and dataset == "mirror" and record is records[-1] else record["LABEL"]
                row = (int(record["ID"]), label,
                       None if record["AMOUNT"] is None else Decimal(record["AMOUNT"]))
                if dataset == "history":
                    row += tuple(record[name] if name != "JOURNAL_SEQUENCE" else Decimal(record[name])
                                 for name in TECH)
                values.append(row)
            _, connection = fixture(dataset=dataset, custom_scope=s, rows=values)
            output = BytesIO()
            summary = run(connection, s, output=output, dataset=dataset, fetch_size=1)
            output.seek(0)
            reader = FrameReader(output, scope_sha256=s.sha256, dataset=dataset)
            engine.ingest(dataset, reader)
            assert reader.summary == summary
        result = engine.finish()
    assert result["state"] == ("divergent" if alter_last_mirror else "verified")
