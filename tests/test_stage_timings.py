from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.stage_timings import (
    StageTrace, measure_stage, stage_trace_from_environment, timed_cursor,
)


IDENTITY = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "native-test-v1", "opaque-generation")
POINT = BoundCheckpoint("synthetic-scope", "synthetic-epoch", IDENTITY, 20)
BINDING = CaptureBinding("synthetic-scope", "synthetic-epoch", "ibmi", "JRN",
                         "s3://synthetic-registry/receiver-authority-v1", POINT)
QUERY_OLD = "01b12345-0001-1234-0000-aaaaaaaaaa01"
QUERY_NEW = "01b12345-0001-1234-0000-aaaaaaaaaa02"
STAGES = ("reader_preflight", "reader_capture", "reader_validate", "raw_publish", "loader_outer_preflight",
          "loader_preflight", "snapshot_clear", "history_execute", "mirror_execute", "checkpoint_cas", "schema_execute")


class Clock:
    def __init__(self):
        self.elapsed = 10.0
        self.wall = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self):
        return self.elapsed

    def utc_now(self):
        return self.wall

    def advance(self, seconds, *, wall_seconds=None):
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds if wall_seconds is None else wall_seconds)


def trace_for(records, clock, **kwargs):
    return StageTrace("reader", emit=records.append, monotonic=clock.monotonic, utc_now=clock.utc_now, **kwargs)


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_stage_measures_elapsed_time_and_utc_without_claiming_a_clock_bound():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    with trace.stage("reader_capture"):
        clock.advance(0.125)
    event = records[0]
    assert event["schema"] == "quadringent.stage-timing.v1"
    assert event["record_type"] == "stage" and event["component"] == "reader"
    assert event["stage"] == "reader_capture" and event["status"] == "completed"
    assert event["started_at"] == "2026-01-01T00:00:00Z"
    assert event["ended_at"] == "2026-01-01T00:00:00.125000Z"
    assert event["duration_ms"] == pytest.approx(125)
    assert event["clock_error_bound_ms"] is None and event["wall_clock_regressed"] is False


def test_wall_clock_regression_does_not_change_monotonic_duration():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    with trace.stage("raw_publish"):
        clock.advance(0.25, wall_seconds=-30)
    assert records[0]["duration_ms"] == pytest.approx(250)
    assert records[0]["wall_clock_regressed"] is True
    assert records[0]["ended_at"] < records[0]["started_at"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "SYNTHETIC_CLOCK_SECRET", True])
def test_invalid_monotonic_samples_stay_unknown_and_never_enter_json(bad):
    records, clock = [], Clock()
    trace = StageTrace("reader", emit=records.append, monotonic=lambda: bad, utc_now=clock.utc_now)
    with trace.stage("reader_capture"):
        pass
    assert records[0]["duration_ms"] is None and records[0]["timing_unavailable"] is True
    assert "SYNTHETIC_CLOCK_SECRET" not in json.dumps(records, allow_nan=False)


def test_monotonic_regression_does_not_fabricate_zero_duration():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    with trace.stage("reader_capture"):
        clock.advance(-0.1)
    assert records[0]["duration_ms"] is None and records[0]["timing_unavailable"] is True


def test_unavailable_wall_clock_is_distinct_from_a_synchronized_measurement():
    records, clock = [], Clock()
    trace = StageTrace("reader", emit=records.append, monotonic=clock.monotonic,
                       utc_now=lambda: datetime(2026, 1, 1))
    with trace.stage("reader_capture"):
        clock.advance(0.1)
    assert records[0]["duration_ms"] == pytest.approx(100)
    assert records[0]["started_at"] is None and records[0]["ended_at"] is None
    assert records[0]["timing_unavailable"] is True and records[0]["clock_error_bound_ms"] is None


def test_nested_stage_durations_are_individual_observations_not_an_additive_latency():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    with trace.stage("loader_preflight"):
        clock.advance(0.05)
        with trace.stage("schema_execute"):
            clock.advance(0.1)
    summary = trace.finish()
    assert [record["duration_ms"] for record in records] == pytest.approx([100, 150, 150])
    assert summary["clock_error_bound_ms"] is None


def test_stage_failure_preserves_the_exception_and_never_its_message():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    error = RuntimeError("SYNTHETIC_EXCEPTION_SECRET sql=SELECT private_table password=hidden")
    with pytest.raises(RuntimeError) as caught:
        with trace.stage("reader_validate"):
            clock.advance(0.1)
            raise error
    assert caught.value is error
    assert records[0]["status"] == "failed" and records[0]["error_type"] == "RuntimeError"
    assert "SYNTHETIC_EXCEPTION_SECRET" not in json.dumps(records)
    assert trace.finish()["status"] == "failed"


@pytest.mark.parametrize("stage", STAGES)
def test_only_declared_stage_names_enter_the_structured_record(stage):
    records, clock = [], Clock()
    with trace_for(records, clock).stage(stage):
        pass
    assert records[0]["stage"] == stage


@pytest.mark.parametrize("stage", ["SQL SELECT secret", "", None, "reader_capture\n", "summary"])
def test_unrecognized_stage_names_are_rejected_without_emission(stage):
    records, clock = [], Clock()
    with pytest.raises(ValueError):
        with trace_for(records, clock).stage(stage):
            pytest.fail("invalid stage entered")
    assert records == []


def test_child_correlation_hashes_the_complete_binding_and_checkpoint():
    records, clock = [], Clock()
    trace = trace_for(records, clock, binding=BINDING, table_id="PRIVATE_TABLE_NAME")
    later = replace(POINT, sequence=21)
    child = trace.child(point=later, batch_id="ab" * 16, payload_sha256="cd" * 32)
    with child.stage("raw_publish"):
        clock.advance(0.01)
    with trace.stage("checkpoint_cas"):
        pass
    first, second = records
    assert first["trace_id"] == second["trace_id"]
    assert first["binding_sha256"] == digest(BINDING.to_dict())
    assert first["checkpoint_sha256"] == digest(later.to_dict()) and first["sequence"] == 21
    assert first["table_tag"] == hashlib.sha256(b"PRIVATE_TABLE_NAME").hexdigest()
    assert first["batch_id"] == "ab" * 16 and first["payload_sha256"] == "cd" * 32
    assert second["checkpoint_sha256"] is None and second["batch_id"] is None
    encoded = json.dumps(records)
    for private in ("PRIVATE_TABLE_NAME", "synthetic-scope", "synthetic-epoch", "synthetic-registry", "RCV1", "JLIB"):
        assert private not in encoded


def test_child_does_not_drop_existing_correlation_when_adding_batch_identity():
    records, clock = [], Clock()
    trace = trace_for(records, clock, binding=BINDING)
    child = trace.child(point=POINT).child(batch_id="ab" * 16, payload_sha256="cd" * 32)
    with child.stage("raw_publish"):
        pass
    assert records[0]["checkpoint_sha256"] == digest(POINT.to_dict())


def test_checkpoint_from_another_binding_is_rejected():
    trace = trace_for([], Clock(), binding=BINDING)
    with pytest.raises(ValueError):
        trace.child(point=replace(POINT, capture_epoch="different-epoch"))


@pytest.mark.parametrize("batch_id", ["ab" * 16, "cd" * 32])
def test_batch_identity_accepts_native_32_hex_and_full_sha256(batch_id):
    records, clock = [], Clock()
    with trace_for(records, clock).child(batch_id=batch_id).stage("raw_publish"):
        pass
    assert records[0]["batch_id"] == batch_id


@pytest.mark.parametrize("kwargs", [
    {"batch_id": "private/bucket"}, {"batch_id": "a" * 31}, {"batch_id": True},
    {"payload_sha256": "a" * 63}, {"payload_sha256": "SELECT secret"},
    {"point": POINT.to_dict()},
])
def test_unsafe_correlation_metadata_is_rejected(kwargs):
    with pytest.raises(ValueError):
        trace_for([], Clock()).child(**kwargs)


class Cursor:
    def __init__(self, clock, *, error=None, after=QUERY_NEW):
        self.clock, self.error, self.after = clock, error, after
        self.sfqid = QUERY_OLD
        self.calls = []
        self.description = ("synthetic",)
        self.arraysize = 1
        self.result = object()

    def execute(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.clock.advance(0.075)
        self.sfqid = self.after
        if self.error:
            raise self.error
        return self.result

    def fetchone(self):
        return (7,)

    def fetchmany(self, count=1):
        return [(count,)]

    def fetchall(self):
        return [(7,), (8,)]


def test_cursor_preserves_execute_result_arguments_fetches_and_attribute_delegation():
    records, clock = [], Clock()
    cursor = Cursor(clock)
    trace = StageTrace("loader", emit=records.append, monotonic=clock.monotonic, utc_now=clock.utc_now)
    proxy = trace.cursor(cursor, "history_execute")
    params = {"secret": "SYNTHETIC_PARAM_SECRET"}
    assert proxy.execute("SELECT SYNTHETIC_SQL_SECRET", params, timeout=12) is cursor.result
    assert cursor.calls == [(("SELECT SYNTHETIC_SQL_SECRET", params), {"timeout": 12})]
    assert proxy.fetchone() == (7,) and proxy.fetchmany(3) == [(3,)] and proxy.fetchall() == [(7,), (8,)]
    assert proxy.description is cursor.description
    proxy.arraysize = 10
    assert cursor.arraysize == 10
    assert records[0]["query_id_before"] == QUERY_OLD and records[0]["query_id_after"] == QUERY_NEW
    assert records[0]["duration_ms"] == pytest.approx(75)
    assert "SYNTHETIC_" not in json.dumps(records)


@pytest.mark.parametrize("after,expected", [(QUERY_OLD, None), (QUERY_NEW, QUERY_NEW), (None, None),
                                            ("SYNTHETIC_INVALID_QUERY_SECRET", None)])
def test_failed_execute_never_attributes_the_previous_query_to_the_failure(after, expected):
    records, clock = [], Clock()
    error = RuntimeError("SYNTHETIC_EXECUTE_SECRET")
    cursor = Cursor(clock, error=error, after=after)
    with pytest.raises(RuntimeError) as caught:
        trace_for(records, clock).cursor(cursor, "mirror_execute").execute("private SQL", ("private value",))
    assert caught.value is error
    event = records[0]
    assert event["query_id_before"] == QUERY_OLD and event["query_id_after"] == expected
    assert event["status"] == "failed" and event["error_type"] == "RuntimeError"
    assert "SYNTHETIC_" not in json.dumps(records)


def test_unchanged_query_id_is_not_claimed_as_a_new_successful_query():
    records, clock = [], Clock()
    cursor = Cursor(clock, after=QUERY_OLD)
    trace_for(records, clock).cursor(cursor, "history_execute").execute("SELECT synthetic")
    assert records[0]["query_id_after"] is None


def test_query_id_read_failure_cannot_change_the_underlying_execute_result():
    class UnreadableQueryCursor:
        @property
        def sfqid(self):
            raise RuntimeError("SYNTHETIC_QUERY_PROPERTY_SECRET")

        def execute(self, statement):
            return statement

    records, clock = [], Clock()
    result = trace_for(records, clock).cursor(UnreadableQueryCursor(), "schema_execute").execute("unchanged")
    assert result == "unchanged"
    assert records[0]["query_id_before"] is None and records[0]["query_id_after"] is None


@pytest.mark.parametrize("operation_fails", [False, True])
def test_unreadable_previous_query_id_cannot_attribute_an_unchanged_id(operation_fails):
    error = ValueError("SYNTHETIC_EXECUTE_SECRET")
    result = object()

    class IntermittentQueryCursor:
        reads = 0

        @property
        def sfqid(self):
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError("SYNTHETIC_QUERY_PROPERTY_SECRET")
            return QUERY_OLD

        def execute(self, statement):
            if operation_fails:
                raise error
            return result

    records, clock = [], Clock()
    cursor = trace_for(records, clock).cursor(IntermittentQueryCursor(), "history_execute")
    if operation_fails:
        with pytest.raises(ValueError) as caught:
            cursor.execute("SYNTHETIC_SQL_SECRET")
        assert caught.value is error
    else:
        assert cursor.execute("SYNTHETIC_SQL_SECRET") is result
    assert records[0]["query_id_before"] is None and records[0]["query_id_after"] is None
    assert records[0]["status"] == ("failed" if operation_fails else "completed")
    assert "SYNTHETIC_" not in json.dumps(records)


@pytest.mark.parametrize("operation_fails", [False, True])
def test_observed_empty_previous_query_id_allows_the_first_real_query_id(operation_fails):
    records, clock = [], Clock()
    error = ValueError("SYNTHETIC_EXECUTE_SECRET") if operation_fails else None
    cursor = Cursor(clock, error=error)
    cursor.sfqid = None
    proxy = trace_for(records, clock).cursor(cursor, "history_execute")
    if operation_fails:
        with pytest.raises(ValueError) as caught:
            proxy.execute("SYNTHETIC_SQL_SECRET")
        assert caught.value is error
    else:
        assert proxy.execute("SYNTHETIC_SQL_SECRET") is cursor.result
    assert records[0]["query_id_before"] is None and records[0]["query_id_after"] == QUERY_NEW


def test_invalid_previous_query_id_also_leaves_query_attribution_unknown():
    records, clock = [], Clock()
    cursor = Cursor(clock)
    cursor.sfqid = "SYNTHETIC_INVALID_QUERY_SECRET"
    trace_for(records, clock).cursor(cursor, "history_execute").execute("private SQL")
    assert records[0]["query_id_before"] is None and records[0]["query_id_after"] is None
    assert "SYNTHETIC_" not in json.dumps(records)


@pytest.mark.parametrize("operation_fails", [False, True])
def test_emitter_failure_never_replaces_execute_result_or_exception(operation_fails):
    clock = Clock()
    error = RuntimeError("SYNTHETIC_EXECUTE_SECRET") if operation_fails else None
    cursor = Cursor(clock, error=error)

    def broken_emit(record):
        raise RuntimeError("SYNTHETIC_EMITTER_SECRET")

    trace = StageTrace("loader", emit=broken_emit, monotonic=clock.monotonic, utc_now=clock.utc_now)
    if operation_fails:
        with pytest.raises(RuntimeError) as caught:
            trace.cursor(cursor, "history_execute").execute("secret SQL")
        assert caught.value is error
    else:
        assert trace.cursor(cursor, "history_execute").execute("secret SQL") is cursor.result
    assert trace.finish()["emission_failures"] >= 1


def test_emitter_base_exception_does_not_mask_the_business_exception():
    clock = Clock()
    error = ValueError("SYNTHETIC_BUSINESS_SECRET")

    def broken_emit(record):
        raise SystemExit("SYNTHETIC_SINK_SECRET")

    trace = StageTrace("loader", emit=broken_emit, monotonic=clock.monotonic, utc_now=clock.utc_now)
    with pytest.raises(ValueError) as caught:
        trace.cursor(Cursor(clock, error=error), "history_execute").execute("private SQL")
    assert caught.value is error


def test_dropped_failure_is_still_visible_in_the_shared_summary():
    records, clock = [], Clock()
    trace = trace_for(records, clock, max_records=1)
    with trace.stage("reader_preflight"):
        pass
    with pytest.raises(ValueError):
        with trace.child().stage("reader_capture"):
            raise ValueError("SYNTHETIC_DROPPED_ERROR")
    summary = trace.finish()
    assert summary["status"] == "failed" and summary["dropped_records"] == 1
    assert summary["truncated"] is True
    assert "SYNTHETIC_DROPPED_ERROR" not in json.dumps(records)


def test_children_share_one_budget_and_explicitly_report_all_dropped_records():
    records, clock = [], Clock()
    trace = trace_for(records, clock, max_records=2)
    children = [trace.child(batch_id="ab" * 16), trace.child(batch_id="cd" * 16)]
    for view in (trace, children[0], children[1], trace):
        with view.stage("raw_publish"):
            clock.advance(0.01)
    summary = trace.finish()
    assert len([record for record in records if record["record_type"] == "stage"]) == 2
    assert len([record for record in records if record["record_type"] == "truncation"]) == 1
    assert records[-1]["record_type"] == "summary"
    assert summary["truncated"] is True and summary["dropped_records"] == 2
    assert summary["recorded_stages"] == 2 and summary["max_records"] == 2
    assert len({record["trace_id"] for record in records}) == 1
    assert len(records) == 4


def test_finish_is_shared_and_idempotent_even_if_an_emitter_mutates_its_input():
    records, clock = [], Clock()

    def emit(record):
        records.append(dict(record))
        record["status"] = "SYNTHETIC_MUTATION"

    trace = StageTrace("reader", emit=emit, monotonic=clock.monotonic, utc_now=clock.utc_now)
    child = trace.child()
    with child.stage("reader_capture"):
        clock.advance(0.2)
    first = trace.finish()
    clock.advance(1)
    assert child.finish(failed=True) == first
    assert len(records) == 2 and first["status"] == "completed"
    first["status"] = "caller mutation"
    assert trace.finish()["status"] == "completed"


def test_finish_can_mark_a_failure_that_preceded_the_instrumented_stages():
    records, clock = [], Clock()
    summary = trace_for(records, clock).finish(failed=True)
    assert summary["status"] == "failed" and summary["recorded_stages"] == 0


@pytest.mark.parametrize("kwargs", [{"component": "SQL_SECRET"}, {"max_records": 0}, {"max_records": -1},
                                    {"max_records": True}, {"binding": {}}, {"table_id": 12}])
def test_trace_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        StageTrace(**{"component": "reader", **kwargs})


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "1", "yes", "on", " TRUE ", "true\n", "enabled"])
def test_environment_is_opt_in_and_disabled_helpers_leave_objects_unchanged(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("QUADRINGENT_STAGE_TIMINGS", raising=False)
    else:
        monkeypatch.setenv("QUADRINGENT_STAGE_TIMINGS", value)
    assert stage_trace_from_environment("reader", binding=BINDING, table_id="synthetic") is None
    cursor = object()
    assert timed_cursor(None, cursor, "unvalidated while disabled") is cursor
    with measure_stage(None, "unvalidated while disabled"):
        pass


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_environment_truthy_values_create_a_trace(monkeypatch, value):
    monkeypatch.setenv("QUADRINGENT_STAGE_TIMINGS", value)
    assert isinstance(stage_trace_from_environment("reader", binding=BINDING), StageTrace)


def test_enabled_helpers_use_the_supplied_trace():
    records, clock = [], Clock()
    trace = trace_for(records, clock)
    cursor = Cursor(clock)
    with measure_stage(trace, "loader_preflight"):
        timed_cursor(trace, cursor, "schema_execute").execute("SELECT synthetic")
    assert [record["stage"] for record in records] == ["schema_execute", "loader_preflight"]


def test_default_emitter_writes_a_compact_json_event_without_exception_text(caplog):
    clock = Clock()
    trace = StageTrace("reader", monotonic=clock.monotonic, utc_now=clock.utc_now)
    with caplog.at_level(logging.INFO, logger="quadringent.stage_timings"):
        with trace.stage("reader_capture"):
            clock.advance(0.02)
        trace.finish()
    assert len(caplog.records) == 2
    payload = caplog.records[0].getMessage().removeprefix("stage_timing ")
    assert json.loads(payload)["duration_ms"] == pytest.approx(20)
    assert ": " not in payload
