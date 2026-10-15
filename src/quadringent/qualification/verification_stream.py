"""Bounded, single-pass NDJSON frames; row semantics belong to the verifier.

Deadlines are checked around synchronous I/O and iteration. The underlying
stream must provide its own timeout if an individual operation can block.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import time
from typing import BinaryIO, Callable, Iterable, Iterator


FORMAT_VERSION = "quadringent-native-row-stream-v1"
DATASETS = frozenset({"source", "snapshot", "journal", "positions", "raw", "history", "mirror"})
HARD_MAX_ROWS = 20_000_000
HARD_MAX_BYTES = 16 * 1024**3
HARD_MAX_LINE_BYTES = 8 * 1024**2
HARD_MAX_SECONDS = 7200
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UTC = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(?:Z|\+00:00)")
_HEADER = frozenset({"format_version", "kind", "dataset", "scope_sha256", "started_at"})
_FOOTER = frozenset({"kind", "row_count", "rows_sha256", "completed_at"})


class StreamError(ValueError):
    """A framing, integrity or budget failure without input values."""


@dataclass(frozen=True)
class StreamLimits:
    """Hard caps: 20M rows, 16 GiB total, 8 MiB per line and 7200 seconds."""

    max_rows: int = 2_000_000
    max_bytes: int = 2 * 1024**3
    max_line_bytes: int = 1024**2
    max_seconds: int = 1800

    def __post_init__(self) -> None:
        for value, cap in ((self.max_rows, HARD_MAX_ROWS), (self.max_bytes, HARD_MAX_BYTES),
                           (self.max_line_bytes, HARD_MAX_LINE_BYTES), (self.max_seconds, HARD_MAX_SECONDS)):
            if type(value) is not int or not 1 <= value <= cap:
                raise StreamError("Invalid stream limits")


@dataclass(frozen=True)
class StreamSummary:
    row_count: int
    byte_count: int
    sha256: str
    rows_sha256: str
    started_at: str
    completed_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _scope(scope_sha256: str, dataset: str, limits: StreamLimits) -> None:
    if type(scope_sha256) is not str or _SHA256.fullmatch(scope_sha256) is None:
        raise StreamError("Invalid stream scope")
    if type(dataset) is not str or dataset not in DATASETS:
        raise StreamError("Invalid stream dataset")
    if type(limits) is not StreamLimits:
        raise StreamError("Invalid stream limits")


def _timestamp(value) -> tuple[datetime, str]:
    match = _UTC.fullmatch(value) if type(value) is str else None
    if match is None:
        raise StreamError("Invalid UTC stream timestamp")
    try:
        stamp = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        raise StreamError("Invalid UTC stream timestamp") from None
    return stamp, (match[2] or "").ljust(9, "0")


class _Budget:
    def __init__(self, limits: StreamLimits) -> None:
        self.limits = limits
        self.byte_count = 0
        self.started = self.previous = self._now()

    @staticmethod
    def _now() -> float:
        value = time.monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise StreamError("Invalid stream clock")
        return value

    def check(self) -> None:
        now = self._now()
        if now < self.previous:
            raise StreamError("Invalid stream clock")
        if now - self.started > self.limits.max_seconds:
            raise StreamError("Stream time budget exceeded")
        self.previous = now

    def add(self, size: int) -> None:
        self.check()
        if self.byte_count + size > self.limits.max_bytes:
            raise StreamError("Stream byte budget exceeded")
        self.byte_count += size


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StreamError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise StreamError("Invalid JSON number")


def _parse(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_float=Decimal, parse_constant=_reject_constant)
    except Exception:
        raise StreamError("Invalid JSON frame") from None
    if type(value) is not dict:
        raise StreamError("Invalid JSON frame")
    return value


class FrameReader(Iterator[dict]):
    """Yield rows once; only a validated footer followed by EOF sets summary."""

    def __init__(self, stream: BinaryIO, *, scope_sha256: str, dataset: str,
                 limits: StreamLimits = StreamLimits()) -> None:
        _scope(scope_sha256, dataset, limits)
        self._stream = stream
        self._scope_sha256, self._dataset = scope_sha256, dataset
        self._budget = _Budget(limits)
        self._sha256, self._rows_sha256 = hashlib.sha256(), hashlib.sha256()
        self._started_at = None
        self._row_count = 0
        self._summary: StreamSummary | None = None
        self._failed = False

    @property
    def scope_sha256(self) -> str:
        return self._scope_sha256

    @property
    def dataset(self) -> str:
        return self._dataset

    @property
    def summary(self) -> StreamSummary | None:
        return self._summary

    def __iter__(self) -> FrameReader:
        return self

    def _line(self, *, required=True) -> bytes:
        self._budget.check()
        data = self._stream.readline(self._budget.limits.max_line_bytes + 1)
        self._budget.check()
        if type(data) is not bytes:
            raise StreamError("A binary stream is required")
        if len(data) > self._budget.limits.max_line_bytes:
            raise StreamError("Stream line budget exceeded")
        if not data:
            if required:
                raise StreamError("Truncated row stream")
            return data
        if not data.endswith(b"\n"):
            raise StreamError("Unterminated stream frame")
        self._budget.add(len(data))
        self._sha256.update(data)
        return data

    def __next__(self) -> dict:
        if self._summary is not None:
            raise StopIteration
        if self._failed:
            raise StreamError("Row stream has failed")
        try:
            if self._started_at is None:
                header = _parse(self._line())
                if (set(header) != _HEADER or header["format_version"] != FORMAT_VERSION
                        or header["kind"] != "header" or header["dataset"] != self.dataset
                        or header["scope_sha256"] != self.scope_sha256):
                    raise StreamError("Invalid stream header")
                _timestamp(header["started_at"])
                self._started_at = header["started_at"]
            data = self._line()
            frame = _parse(data)
            self._budget.check()
            if frame.get("kind") == "row":
                if set(frame) != {"kind", "row"} or type(frame["row"]) is not dict:
                    raise StreamError("Invalid row frame")
                if self._row_count >= self._budget.limits.max_rows:
                    raise StreamError("Stream row budget exceeded")
                self._row_count += 1
                self._rows_sha256.update(data)
                return frame["row"]
            if set(frame) != _FOOTER or frame["kind"] != "end":
                raise StreamError("Invalid stream footer")
            if type(frame["row_count"]) is not int or frame["row_count"] != self._row_count:
                raise StreamError("Stream row count differs")
            if (type(frame["rows_sha256"]) is not str or _SHA256.fullmatch(frame["rows_sha256"]) is None
                    or frame["rows_sha256"] != self._rows_sha256.hexdigest()):
                raise StreamError("Stream row digest differs")
            if _timestamp(frame["completed_at"]) < _timestamp(self._started_at):
                raise StreamError("Stream completion precedes its start")
            if self._line(required=False):
                raise StreamError("Data follows the stream footer")
            self._budget.check()
            self._summary = StreamSummary(self._row_count, self._budget.byte_count, self._sha256.hexdigest(),
                                          self._rows_sha256.hexdigest(), self._started_at, frame["completed_at"])
        except Exception:
            self._failed = True
            raise StreamError("Invalid or incomplete row stream") from None
        raise StopIteration


def _json_chunks(value, *, limit: int, active: set[int]):
    if value is None or type(value) in (bool, int, str):
        if type(value) is str and len(value) > limit:
            raise StreamError("Stream line budget exceeded")
        yield json.dumps(value, ensure_ascii=True, allow_nan=False)
    elif type(value) is Decimal:
        if not value.is_finite():
            raise StreamError("Invalid JSON number")
        yield json.dumps(str(value))
    elif type(value) in (dict, list):
        identity = id(value)
        if identity in active:
            raise StreamError("Cyclic JSON value")
        active.add(identity)
        try:
            is_object = type(value) is dict
            yield "{" if is_object else "["
            for index, item in enumerate(value.items() if is_object else value):
                if index:
                    yield ","
                if is_object:
                    key, item = item
                    if type(key) is not str:
                        raise StreamError("JSON object keys must be strings")
                    yield from _json_chunks(key, limit=limit, active=active)
                    yield ":"
                yield from _json_chunks(item, limit=limit, active=active)
            yield "}" if is_object else "]"
        finally:
            active.remove(identity)
    else:
        raise StreamError("Unsupported exact JSON value")


def _encode(frame: dict, budget: _Budget) -> bytes:
    output = bytearray()
    limit = budget.limits.max_line_bytes
    for chunk in _json_chunks(frame, limit=limit, active=set()):
        budget.check()
        if len(output) + len(chunk) + 1 > limit:
            raise StreamError("Stream line budget exceeded")
        output.extend(chunk.encode("ascii"))
    output.extend(b"\n")
    return bytes(output)


def write_stream(stream: BinaryIO, rows: Iterable[dict], *, scope_sha256: str, dataset: str,
                 started_at: str, completed_at: str | Callable[[], str],
                 limits: StreamLimits = StreamLimits()) -> StreamSummary:
    """Write exact frames once; Decimal becomes text and every float is refused."""
    _scope(scope_sha256, dataset, limits)
    start = _timestamp(started_at)
    if not callable(completed_at) and _timestamp(completed_at) < start:
        raise StreamError("Stream completion precedes its start")
    budget = _Budget(limits)
    complete_hash, rows_hash = hashlib.sha256(), hashlib.sha256()

    def emit(frame: dict) -> bytes:
        data = _encode(frame, budget)
        budget.add(len(data))
        offset = 0
        while offset < len(data):
            budget.check()
            count = stream.write(memoryview(data)[offset:])
            if type(count) is not int or not 0 < count <= len(data) - offset:
                raise StreamError("Incomplete stream write")
            offset += count
        budget.check()
        complete_hash.update(data)
        return data

    try:
        iterator = iter(rows)
        emit({"format_version": FORMAT_VERSION, "kind": "header", "dataset": dataset,
              "scope_sha256": scope_sha256, "started_at": started_at})
        row_count = 0
        while True:
            budget.check()
            try:
                row = next(iterator)
            except StopIteration:
                break
            budget.check()
            if type(row) is not dict:
                raise StreamError("Invalid row value")
            if row_count >= limits.max_rows:
                raise StreamError("Stream row budget exceeded")
            rows_hash.update(emit({"kind": "row", "row": row}))
            row_count += 1
        end = completed_at() if callable(completed_at) else completed_at
        if _timestamp(end) < start:
            raise StreamError("Stream completion precedes its start")
        emit({"kind": "end", "row_count": row_count, "rows_sha256": rows_hash.hexdigest(), "completed_at": end})
        budget.check()
        return StreamSummary(row_count, budget.byte_count, complete_hash.hexdigest(), rows_hash.hexdigest(), started_at, end)
    except Exception:
        raise StreamError("Could not write complete row stream") from None
