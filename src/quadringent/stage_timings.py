"""Opt-in elapsed times for CDC stages, without SQL or source data.

These are process-local stage observations, not end-to-end latency. Children
share one budget: at most ``max_records`` stages, one truncation notice and
one root ``finish`` summary. Only ``QUADRINGENT_STAGE_TIMINGS=true`` enables
the environment factory (case insensitive, without surrounding whitespace).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
from threading import Lock
import time
from typing import Callable
from uuid import uuid4

from .capture_binding import CaptureBinding
from .receiver_identity import BoundCheckpoint


SCHEMA = "quadringent.stage-timing.v1"
_STAGES = frozenset({
    "reader_preflight", "reader_capture", "reader_validate", "raw_publish", "loader_outer_preflight",
    "loader_preflight", "snapshot_clear", "history_execute", "mirror_execute", "checkpoint_cas", "schema_execute",
})
_QUERY_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_LOGGER = logging.getLogger(__name__)


def _default_emit(record: dict) -> None:
    _LOGGER.info("stage_timing %s", json.dumps(record, separators=(",", ":"), allow_nan=False))


def _digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _query_id(cursor) -> tuple[bool, str | None]:
    try:
        value = cursor.sfqid
        if value is None:
            return True, None
        if type(value) is str and _QUERY_ID.fullmatch(value):
            return True, value
    except BaseException:
        pass
    return False, None


def _stage_name(name: str) -> None:
    if type(name) is not str or name not in _STAGES:
        raise ValueError("unknown CDC timing stage")


@dataclass
class _TraceState:
    component: str
    emit: Callable[[dict], None]
    monotonic: Callable[[], float]
    utc_now: Callable[[], datetime]
    max_records: int
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    recorded_stages: int = 0
    dropped_records: int = 0
    emission_failures: int = 0
    failed: bool = False
    summary: dict | None = None
    lock: Lock = field(default_factory=Lock)

    def sample(self) -> tuple[float | None, datetime | None]:
        try:
            monotonic = self.monotonic()
            if type(monotonic) not in (int, float) or not math.isfinite(monotonic):
                monotonic = None
        except BaseException:
            monotonic = None
        try:
            wall = self.utc_now()
            if not isinstance(wall, datetime) or wall.tzinfo is None or wall.utcoffset() is None:
                wall = None
            else:
                wall = wall.astimezone(timezone.utc)
        except BaseException:
            wall = None
        return monotonic, wall


class StageTrace:
    """One trace with immutable per-child correlation and a shared record cap.

    Finish the root after all child work. Query IDs are UUID-shaped Snowflake
    IDs; batch IDs are native 32/64-character lowercase hex digests. Missing
    clocks stay null, and no wall-clock synchronization bound is inferred.
    """

    def __init__(self, component: str, *, binding: CaptureBinding | None = None,
                 table_id: str | None = None, emit: Callable[[dict], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], datetime] | None = None, max_records: int = 64) -> None:
        if type(component) is not str or component not in {"reader", "loader"}:
            raise ValueError("invalid CDC timing component")
        if type(max_records) is not int or max_records < 1:
            raise ValueError("timing record budget must be a positive integer")
        if binding is not None and type(binding) is not CaptureBinding:
            raise ValueError("timing binding must be a CaptureBinding")
        if table_id is not None and (type(table_id) is not str or not table_id):
            raise ValueError("timing table identifier must be a non-empty string")
        if not callable(monotonic) or (utc_now is not None and not callable(utc_now)):
            raise ValueError("timing clocks must be callable")
        if emit is not None and not callable(emit):
            raise ValueError("timing emitter must be callable")
        self._state = _TraceState(component, _default_emit if emit is None else emit, monotonic,
                                  (lambda: datetime.now(timezone.utc)) if utc_now is None else utc_now, max_records)
        self._binding = binding
        self._correlation = {
            "table_tag": hashlib.sha256(table_id.encode()).hexdigest() if table_id is not None else None,
            "binding_sha256": _digest(binding.to_dict()) if binding is not None else None,
            "checkpoint_sha256": None, "sequence": None, "batch_id": None, "payload_sha256": None,
        }
        self._started = self._state.sample()
        self._root_correlation = dict(self._correlation)

    @property
    def trace_id(self) -> str:
        return self._state.trace_id

    def child(self, point: BoundCheckpoint | None = None, batch_id: str | None = None,
              payload_sha256: str | None = None) -> StageTrace:
        correlation = dict(self._correlation)
        if point is not None:
            if type(point) is not BoundCheckpoint:
                raise ValueError("timing checkpoint must be a BoundCheckpoint")
            if self._binding is not None:
                self._binding.validate_checkpoint(point)
            correlation.update(checkpoint_sha256=_digest(point.to_dict()), sequence=point.sequence)
        for name, value, pattern in (("batch_id", batch_id, r"(?:[0-9a-f]{32}|[0-9a-f]{64})"),
                                      ("payload_sha256", payload_sha256, r"[0-9a-f]{64}")):
            if value is not None:
                if type(value) is not str or re.fullmatch(pattern, value) is None:
                    raise ValueError("invalid timing digest correlation")
                correlation[name] = value
        child = object.__new__(StageTrace)
        child.__dict__.update(self.__dict__, _correlation=correlation)
        return child

    def _event(self, record_type: str, stage: str, status: str, started, ended,
               *, correlation: dict | None = None, **fields) -> dict:
        start_mono, start_wall = started
        end_mono, end_wall = ended
        elapsed = None if start_mono is None or end_mono is None else end_mono - start_mono
        duration = elapsed * 1000 if elapsed is not None and elapsed >= 0 and math.isfinite(elapsed * 1000) else None
        return {
            "schema": SCHEMA, "record_type": record_type, "trace_id": self.trace_id,
            "component": self._state.component, "stage": stage, "status": status,
            "started_at": start_wall.isoformat().replace("+00:00", "Z") if start_wall is not None else None,
            "ended_at": end_wall.isoformat().replace("+00:00", "Z") if end_wall is not None else None,
            "duration_ms": duration, "clock_error_bound_ms": None,
            "wall_clock_regressed": end_wall < start_wall if start_wall is not None and end_wall is not None else None,
            "timing_unavailable": duration is None or start_wall is None or end_wall is None,
            **(self._correlation if correlation is None else correlation), **fields,
        }

    def _emit(self, record: dict) -> None:
        try:
            # Emit a copy so an injected sink cannot mutate shared bookkeeping.
            self._state.emit(dict(record))
        except BaseException:
            with self._state.lock:
                self._state.emission_failures += 1

    def _record(self, name, started, ended, failed, fields) -> None:
        with self._state.lock:
            if self._state.summary is not None:
                return
            self._state.failed |= failed
            if self._state.recorded_stages < self._state.max_records:
                self._state.recorded_stages += 1
                record = self._event("stage", name, "failed" if failed else "completed", started, ended, **fields)
            else:
                self._state.dropped_records += 1
                if self._state.dropped_records != 1:
                    return
                record = self._event("truncation", "truncation", "completed", started, ended,
                                     max_records=self._state.max_records, dropped_records=1, truncated=True)
        self._emit(record)

    @contextmanager
    def _span(self, name):
        _stage_name(name)
        started = self._state.sample()
        fields = {"error_type": None, "query_id_before": None, "query_id_after": None}
        failed = False
        try:
            yield fields
        except BaseException as error:
            failed = True
            name_of_type = type(error).__name__
            fields["error_type"] = name_of_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name_of_type) else "Exception"
            raise
        finally:
            self._record(name, started, self._state.sample(), failed, fields)

    @contextmanager
    def stage(self, name: str):
        with self._span(name):
            yield

    def cursor(self, cursor, stage: str):
        _stage_name(stage)
        return _TimedCursor(self, cursor, stage)

    def finish(self, failed: bool = False) -> dict:
        """Emit the single shared summary; repeats return its original value."""
        ended = self._state.sample()
        with self._state.lock:
            if self._state.summary is not None:
                return dict(self._state.summary)
            self._state.failed |= bool(failed)
            summary = self._event(
                "summary", "summary", "failed" if self._state.failed else "completed", self._started, ended,
                correlation=self._root_correlation, recorded_stages=self._state.recorded_stages,
                dropped_records=self._state.dropped_records, max_records=self._state.max_records,
                truncated=self._state.dropped_records > 0, emission_failures=self._state.emission_failures,
            )
            self._state.summary = summary
        self._emit(summary)
        with self._state.lock:
            summary["emission_failures"] = self._state.emission_failures
            return dict(summary)


class _TimedCursor:
    def __init__(self, trace, cursor, stage):
        object.__setattr__(self, "_trace", trace)
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_stage", stage)

    def execute(self, *args, **kwargs):
        before_observed, before = _query_id(self._cursor)
        with self._trace._span(self._stage) as fields:
            fields["query_id_before"] = before
            try:
                return self._cursor.execute(*args, **kwargs)
            finally:
                after_observed, after = _query_id(self._cursor)
                # Unknown or unchanged predecessor state cannot prove a new query.
                fields["query_id_after"] = after if before_observed and after_observed and after != before else None

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):
        setattr(self._cursor, name, value)

    def __iter__(self):
        return iter(self._cursor)

    def __next__(self):
        return next(self._cursor)


def measure_stage(trace: StageTrace | None, name: str):
    return nullcontext() if trace is None else trace.stage(name)


def timed_cursor(trace: StageTrace | None, cursor, stage: str):
    return cursor if trace is None else trace.cursor(cursor, stage)


def stage_trace_from_environment(component: str, *, binding: CaptureBinding | None = None,
                                 table_id: str | None = None) -> StageTrace | None:
    """Accept only ``true`` (case insensitive, no trimming); all else is off."""
    if os.environ.get("QUADRINGENT_STAGE_TIMINGS", "").lower() != "true":
        return None
    return StageTrace(component, binding=binding, table_id=table_id)
