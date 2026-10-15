"""Opt-in reader diagnostics preserve native guards and durable capture order."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quadringent.continuous import ContinuousCaptureService, ReceiverPlanningError
from test_bound_capture_runtime import BINDING, IDENTITY, runtime


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def utc_now(self):
        return datetime(2026, 10, 15, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)


class RecordingTrace:
    def __init__(self, clock, records=None, finishes=None, labels=None):
        self.clock = clock
        self.records = [] if records is None else records
        self.finishes = [] if finishes is None else finishes
        self.labels = labels or {}

    def child(self, **labels):
        return RecordingTrace(self.clock, self.records, self.finishes, self.labels | labels)

    @contextmanager
    def stage(self, name):
        started = self.clock.seconds
        failed = True
        try:
            yield
            failed = False
        finally:
            self.records.append(dict(stage=name, duration=self.clock.seconds - started,
                                     failed=failed, **self.labels))

    def finish(self, *, failed=False):
        self.finishes.append(failed)


def traced_runtime(tmp_path, factory, **options):
    original, store, checkpoint, catalog, runner, registry = runtime(tmp_path, **options)
    service = ContinuousCaptureService(
        catalog, runner, original.coordinator, checkpoint, max_entries=10,
        receipted_scans=True, capture_binding=BINDING, receiver_registry=registry,
        utc_now=original.utc_now, stage_trace_factory=factory,
    )
    return SimpleNamespace(service=service, store=store, checkpoint=checkpoint,
                           catalog=catalog, runner=runner, registry=registry)


def delay(monkeypatch, obj, method, clock, seconds):
    original = getattr(obj, method)

    def wrapped(*args, **kwargs):
        clock.seconds += seconds
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, method, wrapped)


def test_delays_are_localized_to_native_stages(monkeypatch, tmp_path):
    clock = Clock()
    trace = RecordingTrace(clock)
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    delay(monkeypatch, state.catalog, "snapshot", clock, 1)
    delay(monkeypatch, state.runner, "capture", clock, 2)
    delay(monkeypatch, state.service.coordinator, "capture_receipted_window_result", clock, 3)

    assert state.service.run_once().status == "published"

    assert [(r["stage"], r["duration"]) for r in trace.records] == [
        ("reader_preflight", 2), ("reader_capture", 2),
        ("reader_validate", 1), ("raw_publish", 3),
    ]
    assert not any(r["failed"] for r in trace.records)
    assert trace.finishes == [False]
    assert state.checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)


def test_native_generation_failure_never_publishes_with_tracing(tmp_path):
    trace = RecordingTrace(Clock())
    state = traced_runtime(tmp_path, lambda: trace, mutate=True, nonempty=True)

    with pytest.raises(ReceiverPlanningError, match="native receiver generation changed"):
        state.service.run_once()

    assert state.runner.calls == 1
    assert state.checkpoint.load_bound() == BINDING.bootstrap
    assert state.store.list_receipt_keys(10) == ()
    assert [r["stage"] for r in trace.records] == [
        "reader_preflight", "reader_capture", "reader_validate",
    ]
    assert trace.records[-1]["failed"] is True
    assert trace.finishes == [True]


def test_preflight_native_failure_finishes_before_capture(tmp_path):
    trace = RecordingTrace(Clock())
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    state.catalog.current = replace(IDENTITY, native_generation="different-generation")
    with pytest.raises(ReceiverPlanningError, match="native receiver generation changed"):
        state.service.run_once()
    assert state.runner.calls == 0
    assert state.checkpoint.load_bound() is None
    assert state.store.list_receipt_keys(10) == ()
    assert [(r["stage"], r["failed"]) for r in trace.records] == [("reader_preflight", True)]
    assert trace.finishes == [True]


def test_publication_failure_does_not_advance_past_the_original_boundary(monkeypatch, tmp_path):
    trace = RecordingTrace(Clock())
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    failure = OSError("synthetic private failure text")

    def fail(*_):
        raise failure

    monkeypatch.setattr(state.store, "put_once", fail)
    with pytest.raises(OSError) as caught:
        state.service.run_once()

    assert caught.value is failure
    assert state.checkpoint.load_bound() == BINDING.bootstrap
    assert state.store.list_receipt_keys(10) == ()
    assert trace.records[-1]["stage"] == "raw_publish"
    assert trace.records[-1]["failed"] is True
    assert trace.finishes == [True]


def test_idle_finishes_without_capture_or_delivery_stages(tmp_path):
    trace = RecordingTrace(Clock())
    state = traced_runtime(tmp_path, lambda: trace, end=1)

    assert state.service.run_once().status == "idle"

    assert state.runner.calls == 0
    assert [r["stage"] for r in trace.records] == ["reader_preflight"]
    assert trace.finishes == [False]
    assert state.store.list_receipt_keys(10) == ()


@pytest.mark.parametrize("factory_failure", [False, True])
def test_absent_or_failed_diagnostic_factory_preserves_capture(tmp_path, factory_failure):
    def factory():
        raise RuntimeError("synthetic private factory detail")

    state = traced_runtime(tmp_path, factory if factory_failure else None, nonempty=True)
    assert state.service.run_once().status == "published"
    assert state.checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)


def test_factory_is_called_once_for_each_poll(tmp_path):
    traces = []

    def factory():
        traces.append(RecordingTrace(Clock()))
        return traces[-1]

    state = traced_runtime(tmp_path, factory, nonempty=True)
    assert state.service.run_once().status == "published"
    assert state.service.run_once().status == "idle"
    assert len(traces) == 2
    assert [t.finishes for t in traces] == [[False], [False]]


def test_finish_error_does_not_mask_the_native_guard(monkeypatch, tmp_path):
    trace = RecordingTrace(Clock())

    def broken_finish(**_):
        raise RuntimeError("synthetic private diagnostic failure")

    monkeypatch.setattr(trace, "finish", broken_finish)
    state = traced_runtime(tmp_path, lambda: trace, mutate=True, nonempty=True)
    with pytest.raises(ReceiverPlanningError, match="native receiver generation changed"):
        state.service.run_once()
    assert state.checkpoint.load_bound() == BINDING.bootstrap
    assert state.store.list_receipt_keys(10) == ()


def test_published_stage_labels_use_the_bound_batch_identity(tmp_path):
    trace = RecordingTrace(Clock())
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    state.service.run_once()
    receipt = json.loads(state.store.get(state.store.list_receipt_keys(10)[0]))
    published = trace.records[-1]
    assert published["point"] == BINDING.checkpoint(IDENTITY, 3)
    assert published["payload_sha256"] == receipt["raw"]["payload_sha256"]
    assert published["batch_id"] == receipt["raw"]["payload_key"][6:-6]
    assert all("payload" not in r and "manifest" not in r for r in trace.records)


def test_opt_in_preserves_source_guard_and_raw_checkpoint_order(monkeypatch, tmp_path):
    def run(root, factory):
        state = traced_runtime(root, factory, nonempty=True)
        calls = []
        with monkeypatch.context() as patch:
            for obj, method in (
                (type(state.checkpoint), "load_bound"), (type(state.checkpoint), "compare_and_set_bound"),
                (state.registry, "require"), (state.registry, "claim"),
                (state.catalog, "identity"), (state.catalog, "snapshot"),
                (state.runner, "capture"),
                (state.service.coordinator, "capture_receipted_window_result"),
            ):
                original = getattr(obj, method)

                def wrapped(*args, _method=method, _original=original, **kwargs):
                    calls.append(_method)
                    return _original(*args, **kwargs)

                patch.setattr(obj, method, wrapped)
            result = state.service.run_once()
            artifacts = {str(p.relative_to(root / "raw")): p.read_bytes()
                         for p in (root / "raw").rglob("*") if p.is_file()}
            return calls, result, artifacts, state.checkpoint.load_bound()

    baseline = run(tmp_path / "off", None)
    trace = RecordingTrace(Clock())
    assert run(tmp_path / "on", lambda: trace) == baseline


def test_real_trace_has_opaque_correlation_without_source_data(tmp_path):
    import hashlib
    from quadringent.stage_timings import StageTrace

    clock = Clock()
    records = []
    trace = StageTrace("reader", binding=BINDING, table_id="private-table-sentinel",
                       emit=records.append, monotonic=clock.monotonic, utc_now=clock.utc_now)
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    state.service.run_once()

    assert len(records) == 5
    assert len({r["trace_id"] for r in records}) == 1
    assert all(r["component"] == "reader" and r["clock_error_bound_ms"] is None for r in records)
    assert all(r["binding_sha256"] == hashlib.sha256(json.dumps(
        BINDING.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest() for r in records)
    published = next(r for r in records if r["stage"] == "raw_publish")
    point = BINDING.checkpoint(IDENTITY, 3)
    assert published["checkpoint_sha256"] == hashlib.sha256(json.dumps(
        point.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert published["sequence"] == 3
    receipt = json.loads(state.store.get(state.store.list_receipt_keys(10)[0]))
    assert published["payload_sha256"] == receipt["raw"]["payload_sha256"]
    assert published["batch_id"] == receipt["raw"]["payload_key"][6:-6]
    serialized = json.dumps(records)
    assert all(value not in serialized for value in ("private-table-sentinel", "RCVA", "JRN", "TABLE"))


@pytest.mark.parametrize("source_failure", [False, True])
def test_emitter_failure_preserves_success_or_original_source_error(tmp_path, source_failure):
    from quadringent.stage_timings import StageTrace

    def broken_emit(_):
        raise RuntimeError("synthetic private emitter detail")

    trace = StageTrace("reader", binding=BINDING, emit=broken_emit)
    state = traced_runtime(tmp_path, lambda: trace, mutate=source_failure, nonempty=True)
    if source_failure:
        with pytest.raises(ReceiverPlanningError, match="native receiver generation changed"):
            state.service.run_once()
        assert state.checkpoint.load_bound() == BINDING.bootstrap
        assert state.store.list_receipt_keys(10) == ()
    else:
        assert state.service.run_once().status == "published"
        assert state.checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)


def test_truncation_budget_does_not_stop_durable_capture(tmp_path):
    from quadringent.stage_timings import StageTrace

    records = []
    trace = StageTrace("reader", binding=BINDING, emit=records.append, max_records=2)
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    assert state.service.run_once().status == "published"
    assert [r["record_type"] for r in records] == ["stage", "stage", "truncation", "summary"]
    assert records[-1]["dropped_records"] == 2
    assert state.checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)


def test_publication_error_records_only_its_type(monkeypatch, tmp_path):
    from quadringent.stage_timings import StageTrace

    records = []
    trace = StageTrace("reader", binding=BINDING, emit=records.append)
    state = traced_runtime(tmp_path, lambda: trace, nonempty=True)
    original = OSError("private-error-detail")

    def fail(*_):
        raise original

    monkeypatch.setattr(state.store, "put_once", fail)
    with pytest.raises(OSError) as caught:
        state.service.run_once()
    assert caught.value is original
    failed = next(r for r in records if r["stage"] == "raw_publish")
    assert failed["error_type"] == "OSError" and failed["status"] == "failed"
    assert records[-1]["status"] == "failed"
    assert "private-error-detail" not in json.dumps(records)


@pytest.fixture
def timing_logger():
    import logging

    logger = logging.getLogger("quadringent.stage_timings")
    previous = logger.handlers, logger.level, logger.propagate
    logger.handlers = []
    logger.setLevel(logging.ERROR)
    logger.propagate = True
    try:
        yield logger
    finally:
        handlers = logger.handlers
        logger.handlers, level, logger.propagate = previous
        logger.setLevel(level)
        for handler in handlers:
            handler.close()


@pytest.mark.parametrize("flag", [None, "", "false", "1", "yes", "on", " true "])
def test_entrypoint_opt_out_does_not_configure_logging(monkeypatch, flag, timing_logger):
    import logging
    import as400_continuous_capture as entrypoint

    if flag is None:
        monkeypatch.delenv("QUADRINGENT_STAGE_TIMINGS", raising=False)
    else:
        monkeypatch.setenv("QUADRINGENT_STAGE_TIMINGS", flag)
    assert entrypoint._reader_stage_trace(BINDING) is None
    assert timing_logger.handlers == []
    assert timing_logger.level == logging.ERROR and timing_logger.propagate is True


def test_entrypoint_opt_in_emits_one_bounded_trace_per_poll(monkeypatch, tmp_path, capsys, timing_logger):
    import as400_continuous_capture as entrypoint

    monkeypatch.setenv("QUADRINGENT_STAGE_TIMINGS", "true")
    state = traced_runtime(tmp_path, lambda: entrypoint._reader_stage_trace(BINDING), nonempty=True)
    assert state.service.run_once().status == "published"
    assert state.service.run_once().status == "idle"
    assert len(timing_logger.handlers) == 1
    records = [json.loads(line.removeprefix("stage_timing "))
               for line in capsys.readouterr().err.splitlines()]
    assert len(records) == 7
    assert len({r["trace_id"] for r in records}) == 2
    assert [r["record_type"] for r in records].count("summary") == 2
    assert all(r["clock_error_bound_ms"] is None for r in records)
    assert timing_logger.propagate is False
