from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json

import pytest

from quadringent.qualification.verification_stream import (
    FrameReader, StreamError, StreamLimits, write_stream,
)


SCOPE = "a" * 64
START = "2026-10-16T00:00:00.000000001Z"
END = "2026-10-16T00:00:00.000000002+00:00"
FORMAT = "quadringent-native-row-stream-v1"


def line(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def framed(rows=(), *, header=None, footer=None):
    row_lines = b"".join(line({"kind": "row", "row": row}) for row in rows)
    first = {"format_version": FORMAT, "kind": "header", "dataset": "source",
             "scope_sha256": SCOPE, "started_at": START}
    last = {"kind": "end", "row_count": len(rows), "rows_sha256": hashlib.sha256(row_lines).hexdigest(),
            "completed_at": END}
    first.update(header or {})
    last.update(footer or {})
    return line(first) + row_lines + line(last)


def reader(data, **kwargs):
    return FrameReader(io.BytesIO(data), scope_sha256=SCOPE, dataset="source", **kwargs)


def test_reader_yields_once_and_publishes_summary_only_after_real_eof():
    data = framed([{"ID": 1}, {"ID": 2}])
    stream = reader(data)
    assert iter(stream) is stream
    assert stream.scope_sha256 == SCOPE and stream.dataset == "source"
    assert stream.summary is None
    assert next(stream) == {"ID": 1}
    assert stream.summary is None
    assert next(stream) == {"ID": 2}
    assert stream.summary is None
    assert list(stream) == []
    summary = stream.summary
    assert summary.row_count == 2 and summary.byte_count == len(data)
    assert summary.sha256 == hashlib.sha256(data).hexdigest()
    assert summary.rows_sha256 == hashlib.sha256(b"".join(data.splitlines(keepends=True)[1:-1])).hexdigest()
    assert summary.started_at == START and summary.completed_at == END
    assert summary.to_dict()["row_count"] == 2
    assert list(stream) == [] and stream.summary is summary
    for name in ("scope_sha256", "dataset", "summary"):
        with pytest.raises(AttributeError):
            setattr(stream, name, None)


def test_writer_is_deterministic_and_matches_reader_hashes_with_exact_decimals():
    class Once:
        used = False

        def __iter__(self):
            assert not self.used
            self.used = True
            yield {"ID": 1, "AMOUNT": Decimal("12345678901234567890.001200"),
                   "NESTED": [Decimal("1E-30"), True, None], "TEXT": "é\n"}

    output = io.BytesIO()
    calls = []
    summary = write_stream(output, Once(), scope_sha256=SCOPE, dataset="history", started_at=START,
                           completed_at=lambda: calls.append(True) or END)
    assert calls == [True]
    raw = output.getvalue()
    decoded = FrameReader(io.BytesIO(raw), scope_sha256=SCOPE, dataset="history")
    rows = list(decoded)
    assert rows == [{"ID": 1, "AMOUNT": "12345678901234567890.001200",
                     "NESTED": ["1E-30", True, None], "TEXT": "é\n"}]
    assert decoded.summary == summary
    again = io.BytesIO()
    assert write_stream(again, Once(), scope_sha256=SCOPE, dataset="history", started_at=START,
                        completed_at=END) == summary
    assert again.getvalue() == raw


def test_original_number_bytes_are_parsed_as_decimal_without_float_rounding():
    row = b'{"kind":"row","row":{"amount":12345678901234567890.123456789}}\r\n'
    data = line({"format_version": FORMAT, "kind": "header", "dataset": "source",
                 "scope_sha256": SCOPE, "started_at": START}) + row + line({
                     "kind": "end", "row_count": 1, "rows_sha256": hashlib.sha256(row).hexdigest(),
                     "completed_at": END})
    stream = reader(data)
    assert list(stream) == [{"amount": Decimal("12345678901234567890.123456789")}]
    assert stream.summary.rows_sha256 == hashlib.sha256(row).hexdigest()


@pytest.mark.parametrize("dataset", ["source", "snapshot", "journal", "positions", "raw", "history", "mirror"])
def test_all_declared_datasets_accept_an_empty_complete_stream(dataset):
    output = io.BytesIO()
    written = write_stream(output, iter(()), scope_sha256=SCOPE, dataset=dataset,
                           started_at=START, completed_at=END)
    decoded = FrameReader(io.BytesIO(output.getvalue()), scope_sha256=SCOPE, dataset=dataset)
    assert list(decoded) == []
    assert decoded.summary == written and written.row_count == 0


@pytest.mark.parametrize("changes", [
    {"dataset": "other"}, {"scope_sha256": "b" * 64}, {"kind": "row"},
    {"format_version": "other"}, {"extra": True},
])
def test_foreign_or_nonexact_header_never_establishes_summary(changes):
    stream = reader(framed(header=changes))
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None


@pytest.mark.parametrize("changes", [
    {"row_count": True}, {"row_count": 1}, {"row_count": 0.0}, {"row_count": -1},
    {"rows_sha256": "0" * 64}, {"rows_sha256": "A" * 64}, {"extra": "no"},
])
def test_invalid_footer_never_establishes_summary(changes):
    stream = reader(framed(footer=changes))
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None


@pytest.mark.parametrize("data", [
    b"", b"{}\n", framed()[:-1], framed().splitlines(keepends=True)[0],
    framed() + b"\n", framed() + b'{"kind":"row","row":{}}\n',
    framed([{"ID": 1}]).replace(b'"ID":1', b'"ID":2'),
    framed().replace(b'"kind":"header"', b'"kind":"header","kind":"header"'),
    framed([{"ID": 1}]).replace(b'"ID":1', b'"ID":1,"ID":1'),
    framed([{"ID": 1}]).replace(b'"ID":1', b'"ID":NaN'),
    framed([{"ID": 1}]).replace(b'"ID":1', b'"ID":Infinity'),
    framed([{"ID": 1}]).replace(b'"ID":1', b'"ID":-Infinity'),
    framed([{"ID": 1}]).replace(b'"row":{"ID":1}', b'"row":[]'),
])
def test_corruption_truncation_duplicates_constants_and_trailing_bytes_fail_closed(data):
    stream = reader(data)
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None
    with pytest.raises(StreamError):
        next(stream)


@pytest.mark.parametrize("stamp", [
    "2026-10-16", "2026-10-16T00:00:00", "2026-10-16 00:00:00Z",
    "2026-10-16T00:00:00+02:00", "2026-02-30T00:00:00Z",
    "2026-10-16T24:00:00Z", "2026-10-16T00:00:00.1234567890Z", None,
])
def test_timestamps_require_valid_strict_utc_and_nanoseconds_at_most(stamp):
    for data in (framed(header={"started_at": stamp}), framed(footer={"completed_at": stamp})):
        stream = reader(data)
        with pytest.raises(StreamError):
            list(stream)
        assert stream.summary is None


def test_submicrosecond_end_before_start_is_rejected():
    stream = reader(framed(footer={"completed_at": "2026-10-16T00:00:00Z"}))
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None


@pytest.mark.parametrize("name", ["max_rows", "max_bytes", "max_line_bytes", "max_seconds"])
@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1", None, 10**30])
def test_limits_are_strict_positive_bounded_integers(name, bad):
    with pytest.raises(StreamError):
        StreamLimits(**{name: bad})


def test_reader_bounds_every_readline_and_rejects_oversized_line_without_reading_it_all():
    class Bounded(io.BytesIO):
        requests = []

        def readline(self, size=-1):
            self.requests.append(size)
            assert size == 33
            return super().readline(size)

    source = Bounded(b"x" * 1000 + b"\n")
    stream = FrameReader(source, scope_sha256=SCOPE, dataset="source", limits=StreamLimits(max_line_bytes=32))
    with pytest.raises(StreamError):
        list(stream)
    assert source.tell() == 33 and source.requests == [33]


def test_global_reader_row_and_byte_budgets_include_header_and_footer():
    data = framed([{}, {}])
    for limits in (StreamLimits(max_rows=1), StreamLimits(max_bytes=len(data) - 1)):
        stream = reader(data, limits=limits)
        with pytest.raises(StreamError):
            list(stream)
        assert stream.summary is None
    stream = reader(data, limits=StreamLimits(max_rows=2, max_bytes=len(data)))
    assert list(stream) == [{}, {}]


@pytest.mark.parametrize("value", [0.0, 0.1, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity")])
def test_writer_rejects_float_and_nonfinite_decimal_recursively(value):
    with pytest.raises(StreamError):
        write_stream(io.BytesIO(), [{"nested": [{"amount": value}]}], scope_sha256=SCOPE,
                     dataset="source", started_at=START, completed_at=END)


def test_writer_does_not_hide_duplicate_key_risk_by_coercing_nonstring_keys():
    with pytest.raises(StreamError):
        write_stream(io.BytesIO(), [{1: "a", "1": "b"}], scope_sha256=SCOPE,
                     dataset="source", started_at=START, completed_at=END)


def test_writer_supports_partial_writes_and_sanitizes_io_and_producer_failures():
    class Partial(io.BytesIO):
        def write(self, data):
            return super().write(data[:7])

    output = Partial()
    summary = write_stream(output, [{}], scope_sha256=SCOPE, dataset="source", started_at=START, completed_at=END)
    decoded = reader(output.getvalue())
    assert list(decoded) == [{}] and decoded.summary == summary

    class Broken:
        def write(self, data):
            raise OSError("SYNTHETIC_PRIVATE_VALUE")

    def broken_rows():
        yield {}
        raise RuntimeError("SYNTHETIC_PRIVATE_VALUE")

    for destination, rows in ((Broken(), [{}]), (io.BytesIO(), broken_rows())):
        with pytest.raises(StreamError) as caught:
            write_stream(destination, rows, scope_sha256=SCOPE, dataset="source", started_at=START, completed_at=END)
        assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value)


def test_global_monotonic_budget_includes_time_between_rows(monkeypatch):
    from quadringent.qualification import verification_stream as module
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    stream = reader(framed([{}, {}]), limits=StreamLimits(max_seconds=1))
    assert next(stream) == {}
    clock[0] += 2
    with pytest.raises(StreamError):
        next(stream)
    assert stream.summary is None

    def slow_rows():
        yield {}
        clock[0] += 2
        yield {}

    with pytest.raises(StreamError):
        write_stream(io.BytesIO(), slow_rows(), scope_sha256=SCOPE, dataset="source", started_at=START,
                     completed_at=END, limits=StreamLimits(max_seconds=1))


def test_footer_is_consumed_and_eof_confirmed_before_summary():
    class DelayedExtra(io.BytesIO):
        def readline(self, size=-1):
            data = super().readline(size)
            return data if data else b"x"

    stream = FrameReader(DelayedExtra(framed([{}])), scope_sha256=SCOPE, dataset="source")
    assert next(stream) == {}
    assert stream.summary is None
    with pytest.raises(StreamError):
        next(stream)
    assert stream.summary is None


def test_timezone_equivalent_utc_forms_and_equal_start_end_are_accepted():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    for stamp in (now.isoformat(), (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")):
        stream = reader(framed(header={"started_at": stamp}, footer={"completed_at": stamp}))
        assert list(stream) == [] and stream.summary.started_at == stamp


def test_row_business_shape_is_not_restricted_to_known_columns_or_shallow_objects():
    nested = {"UNRECOGNIZED_COLUMN": {"value": []}}
    for _ in range(130):
        nested = {"nested": nested}
    output = io.BytesIO()
    write_stream(output, [nested], scope_sha256=SCOPE, dataset="source", started_at=START, completed_at=END)
    assert list(reader(output.getvalue())) == [nested]


@pytest.mark.parametrize("kind", ["rows", "bytes", "line"])
def test_writer_enforces_every_global_budget_before_publishing_a_footer(kind):
    output = io.BytesIO()
    rows = [{"VALUE": "x" * 500}, {}]
    if kind == "rows":
        limits = StreamLimits(max_rows=1)
    elif kind == "bytes":
        limits = StreamLimits(max_bytes=250)
    else:
        limits = StreamLimits(max_line_bytes=300)
    with pytest.raises(StreamError):
        write_stream(output, rows, scope_sha256=SCOPE, dataset="source", started_at=START,
                     completed_at=END, limits=limits)
    assert b'"kind":"end"' not in output.getvalue()


def test_line_budget_includes_newline_and_accepts_the_exact_boundary():
    data = framed([{"VALUE": "x" * 500}])
    longest = max(map(len, data.splitlines(keepends=True)))
    assert list(reader(data, limits=StreamLimits(max_line_bytes=longest))) == [{"VALUE": "x" * 500}]
    stream = reader(data, limits=StreamLimits(max_line_bytes=longest - 1))
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None


def test_reader_sanitizes_io_and_invalid_utf8_failures():
    class Broken:
        def readline(self, size):
            raise OSError("SYNTHETIC_PRIVATE_VALUE")

    for source in (Broken(), io.BytesIO(b"\xff\n"), io.StringIO("private input\n")):
        stream = FrameReader(source, scope_sha256=SCOPE, dataset="source")
        with pytest.raises(StreamError) as caught:
            list(stream)
        assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value)
        assert "private input" not in str(caught.value)
        assert stream.summary is None


@pytest.mark.parametrize("result", [0, None, True, -1, 10**9])
def test_invalid_binary_write_counts_are_refused(result):
    class InvalidWrite:
        def write(self, data):
            return result

    with pytest.raises(StreamError):
        write_stream(InvalidWrite(), [], scope_sha256=SCOPE, dataset="source",
                     started_at=START, completed_at=END)


def test_time_budget_includes_blocking_calls_returning_and_the_final_eof_probe(monkeypatch):
    from quadringent.qualification import verification_stream as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowEOF(io.BytesIO):
        def readline(self, size):
            data = super().readline(size)
            if not data:
                clock[0] += 2
            return data

    stream = FrameReader(SlowEOF(framed()), scope_sha256=SCOPE, dataset="source",
                         limits=StreamLimits(max_seconds=1))
    with pytest.raises(StreamError):
        list(stream)
    assert stream.summary is None

    class SlowWrite(io.BytesIO):
        def write(self, data):
            clock[0] += 2
            return super().write(data)

    with pytest.raises(StreamError):
        write_stream(SlowWrite(), [], scope_sha256=SCOPE, dataset="source", started_at=START,
                     completed_at=END, limits=StreamLimits(max_seconds=1))


@pytest.mark.parametrize("scope,dataset", [("b" * 63, "source"), ("A" * 64, "source"),
                                          (True, "source"), (SCOPE, "other"), (SCOPE, True)])
def test_expected_scope_and_dataset_are_validated_without_input_in_errors(scope, dataset):
    with pytest.raises(StreamError):
        FrameReader(io.BytesIO(), scope_sha256=scope, dataset=dataset)
    with pytest.raises(StreamError):
        write_stream(io.BytesIO(), [], scope_sha256=scope, dataset=dataset, started_at=START, completed_at=END)


def test_limits_defaults_and_explicit_hardcaps():
    assert StreamLimits() == StreamLimits(max_rows=2_000_000, max_bytes=2 * 1024**3,
                                          max_line_bytes=1024**2, max_seconds=1800)
    for name, cap in {"max_rows": 20_000_000, "max_bytes": 16 * 1024**3,
                      "max_line_bytes": 8 * 1024**2, "max_seconds": 7200}.items():
        assert getattr(StreamLimits(**{name: cap}), name) == cap
        with pytest.raises(StreamError):
            StreamLimits(**{name: cap + 1})
