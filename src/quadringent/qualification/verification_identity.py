"""Read effective Snowflake identity; observations never constitute admission.

Originals encode DBAPI values and metadata, not Snowflake wire bytes. The caller
must supervise blocking DBAPI calls with an outer deadline; network_timeout does
not bound every result fetch or SDK retry. Table populations are checked by the
destination exporter, not by these session functions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import time
from types import MappingProxyType
from typing import Callable, Mapping
import unicodedata

from .verification_contract import VerificationScope


_FIELDS = ("account_locator", "organization_name", "account_name", "role", "warehouse",
           "database", "schema", "session", "user")
_FUNCTIONS = ("CURRENT_ACCOUNT", "CURRENT_ORGANIZATION_NAME", "CURRENT_ACCOUNT_NAME", "CURRENT_ROLE",
              "CURRENT_WAREHOUSE", "CURRENT_DATABASE", "CURRENT_SCHEMA", "CURRENT_SESSION", "CURRENT_USER")
IDENTITY_SQL = "SELECT " + ", ".join(f'{function}() AS "{name}"' for function, name in zip(_FUNCTIONS, _FIELDS, strict=True))
_FORMAT = "quadringent-snowflake-session-identity-v1"
_MAX_BYTES = 65536
_QUERY_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class DestinationIdentityError(ValueError):
    """The session identity observation is unavailable or inconsistent."""


def _require(condition):
    if not condition:
        raise DestinationIdentityError("destination_identity_failed")


def _text(value):
    _require(type(value) is str and 1 <= len(value) <= 256 and value.strip() == value
             and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value))
    return value


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


@dataclass(frozen=True)
class DestinationIdentityExpectation:
    account_locator: str
    organization_name: str
    account_name: str
    role: str
    warehouse: str
    database: str
    schema: str
    user: str

    def __post_init__(self):
        for value in asdict(self).values():
            _text(value)


@dataclass(frozen=True)
class DestinationIdentityObservation:
    scope_sha256: str
    query_id: str | None
    started_at: str
    completed_at: str
    _values: tuple = field(repr=False)
    _content: bytes = field(repr=False)

    @property
    def values(self) -> Mapping[str, str | int | Decimal]:
        return MappingProxyType(dict(zip(_FIELDS, self._values, strict=True)))

    @property
    def original_bytes(self) -> bytes:
        return self._content

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._content).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self._content)


def _binding(scope, expected):
    _require(type(scope) is VerificationScope and type(expected) is DestinationIdentityExpectation)
    values = asdict(expected)
    for value in values.values():
        _text(value)
    _require((expected.database, expected.schema) == (scope.destination_database, scope.destination_schema))
    destination = dict(database=scope.destination_database, schema=scope.destination_schema,
                       history_table=scope.history_table, mirror_table=scope.mirror_table)
    return destination, hashlib.sha256(_canonical(values)).hexdigest()


def _metadata(description):
    _require(type(description) in (tuple, list) and len(description) == len(_FIELDS))
    result = []
    for item, name in zip(description, _FIELDS, strict=True):
        _require(isinstance(item, (tuple, list)) and len(item) == 7 and item[0] == name)
        _require(type(item[1]) is int and item[1] == (0 if name == "session" else 2))
        for value in item[2:6]:
            _require(value is None or type(value) is int and 0 <= value <= 2**63 - 1)
        _require(item[6] is None or type(item[6]) is bool)
        if name == "session":
            _require(type(item[4]) is int and 1 <= item[4] <= 38 and type(item[5]) is int and item[5] == 0)
        else:
            _require(item[4] is None and item[5] is None)
        result.append(list(item))
    return result


def _session(value):
    if type(value) is Decimal:
        _require(value.is_finite() and -38 <= value.as_tuple().exponent <= 38
                 and len(value.as_tuple().digits) <= 38 and value == value.to_integral_value())
    else:
        _require(type(value) is int)
    _require(0 < value < 10**38)
    return value


def _typed(values, expected):
    _require(type(values) in (list, tuple) and len(values) == len(_FIELDS))
    records = []
    for name, value in zip(_FIELDS, values, strict=True):
        if name == "session":
            _session(value)
            records.append(dict(type="decimal" if type(value) is Decimal else "int", value=str(value)))
        else:
            _text(value)
            _require(value == getattr(expected, name))
            records.append(dict(type="str", value=value))
    return records


def _values(records):
    _require(type(records) is list and len(records) == len(_FIELDS))
    values = []
    for name, record in zip(_FIELDS, records, strict=True):
        _require(type(record) is dict and set(record) == {"type", "value"})
        value = record["value"]
        if name != "session":
            _require(record["type"] == "str")
            values.append(_text(value))
        else:
            _require(type(value) is str and 1 <= len(value) <= 80)
            if record["type"] == "int":
                _require(re.fullmatch(r"[1-9][0-9]{0,37}", value) is not None)
                values.append(_session(int(value)))
            else:
                _require(record["type"] == "decimal")
                number = Decimal(value)
                _require(str(number) == value)
                values.append(_session(number))
    return tuple(values)


def _stamp(value):
    _require(type(value) is str and 1 <= len(value) <= 40)
    stamp = datetime.fromisoformat(value)
    _require(stamp.tzinfo is not None and stamp.utcoffset() == timedelta(0))
    return stamp


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(_value):
    raise DestinationIdentityError("destination_identity_failed")


def read_destination_identity(content: bytes, scope: VerificationScope, *,
                              expected: DestinationIdentityExpectation) -> DestinationIdentityObservation:
    """Recheck an original observation against server expectations, without I/O."""
    try:
        destination, expected_sha = _binding(scope, expected)
        _require(type(content) is bytes and 1 <= len(content) <= _MAX_BYTES)
        document = json.loads(content.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
                              parse_float=Decimal)
        _require(type(document) is dict and set(document) == {
            "format_version", "scope_sha256", "destination", "expectation_sha256", "sql", "started_at", "completed_at",
            "columns", "values", "query_id", "origin", "native_admission", "transaction_consistency_verified"})
        _require(document["format_version"] == _FORMAT and document["scope_sha256"] == scope.sha256
                 and document["destination"] == destination and document["expectation_sha256"] == expected_sha
                 and document["sql"] == IDENTITY_SQL and document["origin"] == "dbapi_typed_result_not_wire_bytes"
                 and document["native_admission"] == "not_evaluated"
                 and document["transaction_consistency_verified"] is False)
        _metadata(document["columns"])
        values = _values(document["values"])
        _require(_typed(values, expected) == document["values"])
        _require(_stamp(document["started_at"]) <= _stamp(document["completed_at"]))
        query = document["query_id"]
        _require(query is None or type(query) is str and _QUERY_ID.fullmatch(query) is not None)
        return DestinationIdentityObservation(scope.sha256, query, document["started_at"], document["completed_at"],
                                              values, content)
    except Exception:
        raise DestinationIdentityError("destination_identity_failed") from None


class _Budget:
    def __init__(self, connection, check_current, seconds):
        _require(type(seconds) is int and 1 <= seconds <= 60 and callable(check_current))
        self.connection, self.current, self.seconds = connection, check_current, seconds
        self.started = self.previous = time.monotonic()
        self.check()

    def _remaining(self):
        now = time.monotonic()
        remaining = self.seconds - (now - self.started)
        _require(math.isfinite(now) and math.isfinite(self.started) and now >= self.previous and remaining > 0)
        self.previous = now
        return remaining

    def check(self):
        self._remaining()
        _require(self.current() is True)
        timeout = self.connection.network_timeout
        remaining = self._remaining()
        _require(type(timeout) is int and 0 < timeout <= remaining)
        return remaining


def _query_id(cursor):
    try:
        value = cursor.sfqid
        if value is None:
            return True, None
        if type(value) is str and _QUERY_ID.fullmatch(value) is not None:
            return True, value
    except Exception:
        pass
    return False, None


def observe_destination_identity(connection, scope: VerificationScope, *,
                                 expected: DestinationIdentityExpectation,
                                 check_current: Callable[[], bool], max_seconds: int = 60) -> DestinationIdentityObservation:
    """Execute one fixed SELECT and close its cursor before returning an original."""
    try:
        destination, expected_sha = _binding(scope, expected)
        budget = _Budget(connection, check_current, max_seconds)
        started = datetime.now(timezone.utc).isoformat()
        budget.check()
        cursor = connection.cursor()
        try:
            budget.check()
            before_observed, before_id = _query_id(cursor)
            seconds = math.floor(budget.check())
            _require(seconds >= 1)
            cursor.execute(IDENTITY_SQL, timeout=seconds)
            budget.check()
            after_observed, after_id = _query_id(cursor)
            query = after_id if before_observed and after_observed and before_id != after_id else None
            columns = _metadata(cursor.description)
            budget.check()
            values = cursor.fetchone()
            budget.check()
            records = _typed(values, expected)
            _require(cursor.fetchone() is None)
            budget.check()
        finally:
            cursor.close()
            budget.check()
        content = _canonical(dict(format_version=_FORMAT, scope_sha256=scope.sha256, destination=destination,
                                  expectation_sha256=expected_sha, sql=IDENTITY_SQL, started_at=started,
                                  completed_at=datetime.now(timezone.utc).isoformat(), columns=columns, values=records,
                                  query_id=query, origin="dbapi_typed_result_not_wire_bytes",
                                  native_admission="not_evaluated", transaction_consistency_verified=False))
        result = read_destination_identity(content, scope, expected=expected)
        budget.check()
        return result
    except Exception:
        raise DestinationIdentityError("destination_identity_failed") from None


def validate_destination_identity_pair(before: DestinationIdentityObservation, after: DestinationIdentityObservation, *,
                                       scope: VerificationScope, expected: DestinationIdentityExpectation) -> None:
    """Validate two originals around an export on one effective SQL session."""
    try:
        _require(type(before) is DestinationIdentityObservation and type(after) is DestinationIdentityObservation)
        first = read_destination_identity(before.original_bytes, scope, expected=expected)
        last = read_destination_identity(after.original_bytes, scope, expected=expected)
        _require(first == before and last == after and first.values == last.values
                 and _stamp(first.completed_at) <= _stamp(last.started_at))
    except Exception:
        raise DestinationIdentityError("destination_identity_failed") from None
