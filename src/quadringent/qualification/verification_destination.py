"""Bounded Snowflake SELECT exports; completion does not establish a stable cut.

Snowflake network_timeout bounds query requests, not all result-chunk retries
or prefetch. Native execution requires a supervised process with an outer
deadline; synchronous DBAPI calls cannot be preempted here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import math
import re
import time

from .verification_contract import VerificationScope
from .verification_stream import StreamLimits, StreamSummary, write_stream


_TECHNICAL = (("EVENT_ID", "text"), ("OPERATION", "text"),
              ("JOURNAL_RECEIVER", "text"), ("JOURNAL_SEQUENCE", "sequence"))
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{9}")
_MAX_FETCH = 10_000
_MAX_COLUMNS = 512
_FETCH_TEXT_BYTES = 16 * 1024**2


class DestinationExportError(ValueError):
    """The destination did not produce a complete admitted stream."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote(value):
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError
    return '"' + value + '"'


class _Budget:
    def __init__(self, connection, limits):
        self.connection = connection
        self.started = self.previous = time.monotonic()
        self.seconds = limits.max_seconds

    def remaining(self):
        now = time.monotonic()
        remaining = self.seconds - (now - self.started)
        if not math.isfinite(now) or now < self.previous or remaining <= 0:
            raise ValueError
        self.previous = now
        # This property bounds query requests, not result-chunk prefetch.
        timeout = self.connection.network_timeout
        if type(timeout) is not int or not 0 < timeout <= remaining:
            raise ValueError
        return remaining

    def query_seconds(self):
        seconds = math.floor(self.remaining())
        if seconds < 1:
            raise ValueError
        return seconds


class _Cursor:
    def __init__(self, connection, budget):
        budget.remaining()
        self.cursor = connection.cursor()
        self.closed = False
        self.budget = budget

    def execute(self, sql):
        self.cursor.execute(sql, timeout=self.budget.query_seconds())
        self.budget.remaining()

    def close(self):
        if not self.closed:
            self.closed = True
            self.cursor.close()
            self.budget.remaining()


def _metadata(cursor, fields):
    description = cursor.description
    if type(description) not in (tuple, list) or len(description) != len(fields):
        raise ValueError
    result = []
    for item, (name, kind) in zip(description, fields, strict=True):
        if not isinstance(item, (tuple, list)) or len(item) != 7 or item[0] != name:
            raise ValueError
        code, precision, scale = item[1], item[4], item[5]
        if type(code) is not int:
            raise ValueError
        if kind in {"int", "decimal", "sequence"} and code == 0:
            if (type(precision) is not int or not 1 <= precision <= 38
                    or type(scale) is not int or not 0 <= scale <= precision
                    or kind != "decimal" and scale != 0):
                raise ValueError
        elif kind in {"text", "sequence"} and code == 2:
            pass
        elif kind == "date" and code == 3:
            pass
        elif kind == "timestamp" and code == 8:
            if type(scale) is not int or not 0 <= scale <= 9:
                raise ValueError
        elif kind == "oversize" and code == 13:
            pass
        else:
            raise ValueError
        result.append((code, precision, scale))
    return result


def _alias(base, names):
    candidate, suffix = base, 0
    while candidate.casefold() in names:
        suffix += 1
        candidate = f"{base}_{suffix}"
    names.add(candidate.casefold())
    return candidate


def _select(table, fields, original, dataset, scope, limit):
    names = {name.casefold() for name, _ in fields}
    size = _alias("_QDT_TEXT_BYTES", names)
    flag = _alias("_QDT_OVERSIZE", names)
    text_names = {name for (name, kind), metadata in zip(fields, original, strict=True)
                  if kind == "text" or kind == "sequence" and metadata[0] == 2}
    text = [_quote(name) for name, _ in fields if name in text_names]
    size_sql = "+".join(f"COALESCE(OCTET_LENGTH({name}),0)" for name in text) or "0"
    projected = []
    for name, kind in fields:
        column = _quote(name)
        if name in text_names:
            projected.append(f"CASE WHEN {_quote(size)}<={limit} THEN {column} ELSE NULL END AS {column}")
        elif kind == "timestamp":
            projected.append(f"TO_CHAR({column},'YYYY-MM-DD\"T\"HH24:MI:SS.FF9') AS {column}")
        else:
            projected.append(column)
    projected.append(f"{_quote(size)}>{limit} AS {_quote(flag)}")
    columns = ",".join(_quote(name) for name, _ in fields)
    order = ("EVENT_ID",) if dataset == "history" else scope.primary_key
    sql = ("SELECT " + ",".join(projected) + " FROM (SELECT " + columns + "," + size_sql
           + " AS " + _quote(size) + " FROM " + table + ") ORDER BY "
           + ",".join(_quote(name) for name in order))
    return sql, flag


def _number(value):
    if type(value) is int:
        if len(str(abs(value))) > 38:
            raise ValueError
        return Decimal(value)
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError
    _, digits, exponent = value.as_tuple()
    if exponent < -38 or len(digits) + max(exponent, 0) > 38:
        raise ValueError
    return value


def _value(value, kind, code):
    if value is None:
        return None
    if kind in {"int", "decimal"} or kind == "sequence" and code == 0:
        number = _number(value)
        if kind in {"int", "sequence"}:
            if number != number.to_integral_value():
                raise ValueError
            return str(int(number)) if kind == "sequence" else int(number)
        return number
    if kind == "date":
        if type(value) is not date:
            raise ValueError
        return value.isoformat()
    if type(value) is not str:
        raise ValueError
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError
    if kind == "timestamp":
        if _STAMP.fullmatch(value) is None:
            raise ValueError
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    return value


def export_destination(connection, scope: VerificationScope, stream, *, dataset: str,
                       limits: StreamLimits = StreamLimits(), fetch_size: int = 1000) -> StreamSummary:
    """Read one query population exactly; require this return and sealed framed EOF.

    Text is guarded on the server before transfer. A query rowcount detects
    incomplete fetching, not source quiescence or a stable native checkpoint.
    """
    current = None
    try:
        if (type(scope) is not VerificationScope or dataset not in {"history", "mirror"}
                or type(limits) is not StreamLimits or type(fetch_size) is not int
                or not 1 <= fetch_size <= _MAX_FETCH or len(scope.columns) > _MAX_COLUMNS):
            raise ValueError
        budget = _Budget(connection, limits)
        budget.remaining()
        started = _utc_now()
        fields = [(column.name, column.kind) for column in scope.columns]
        if dataset == "history":
            fields += _TECHNICAL
        table = ".".join(_quote(name) for name in (scope.destination_database, scope.destination_schema,
                                                 scope.history_table if dataset == "history" else scope.mirror_table))
        columns = ",".join(_quote(name) for name, _ in fields)
        current = _Cursor(connection, budget)
        current.execute("SELECT " + columns + " FROM " + table + " WHERE 1=0")
        original = _metadata(current.cursor, fields)
        current.close()
        current = None
        sql, flag = _select(table, fields, original, dataset, scope, limits.max_line_bytes)
        current = _Cursor(connection, budget)
        current.execute(sql)
        projected = [(name, "text" if kind == "timestamp" else kind) for name, kind in fields]
        metadata = _metadata(current.cursor, projected + [(flag, "oversize")])
        for (_, kind), before, after in zip(fields, original, metadata[:-1], strict=True):
            if kind != "timestamp" and before != after:
                raise ValueError
        expected = current.cursor.rowcount
        if type(expected) is not int or not 0 <= expected <= limits.max_rows:
            raise ValueError
        batch_size = min(fetch_size, max(1, _FETCH_TEXT_BYTES // limits.max_line_bytes))

        def rows():
            count = 0
            while True:
                budget.remaining()
                batch = current.cursor.fetchmany(batch_size)
                budget.remaining()
                if type(batch) not in (list, tuple) or len(batch) > batch_size:
                    raise ValueError
                if not batch:
                    final_count = current.cursor.rowcount
                    if type(final_count) is not int or count != expected or final_count != expected:
                        raise ValueError
                    current.close()
                    return
                for row in batch:
                    budget.remaining()
                    if (type(row) not in (list, tuple) or len(row) != len(fields) + 1
                            or type(row[-1]) is not bool or row[-1] or count >= expected):
                        raise ValueError
                    record = {name: _value(value, kind, code[0]) for (name, kind), code, value
                              in zip(fields, original, row[:-1], strict=True)}
                    count += 1
                    yield record

        summary = write_stream(stream, rows(), scope_sha256=scope.sha256, dataset=dataset,
                               started_at=started, completed_at=_utc_now, limits=limits)
        stream.flush()
        budget.remaining()
        return summary
    except Exception:
        raise DestinationExportError("destination_export_failed") from None
    finally:
        if current is not None and not current.closed:
            try:
                current.close()
            except Exception:
                pass
