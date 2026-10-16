from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import hashlib
import json

import pytest

from quadringent.qualification import verification_identity as identity
from test_native_verification_ledger import scope


FIELDS = ("account_locator", "organization_name", "account_name", "role", "warehouse",
          "database", "schema", "session", "user")
QUERY_ONE = "01234567-89ab-cdef-0123-456789abcdef"
QUERY_TWO = "fedcba98-7654-3210-fedc-ba9876543210"
SECRET = "SYNTHETIC_PRIVATE_FAILURE"


def expectation(**changes):
    values = dict(account_locator="LOCATOR", organization_name="ORG", account_name="ACCOUNT",
                  role="READER", warehouse="READ_WH", database="DEST", schema="TEST", user="SERVICE")
    return identity.DestinationIdentityExpectation(**(values | changes))


def description():
    return [(name, 0 if name == "session" else 2, None, None,
             38 if name == "session" else None, 0 if name == "session" else None, False)
            for name in FIELDS]


class Cursor:
    def __init__(self, *, row=None, metadata=None, extra=False, fault=None, action=None):
        self.rows = [row or ("LOCATOR", "ORG", "ACCOUNT", "READER", "READ_WH", "DEST", "TEST", 42, "SERVICE")]
        if extra:
            self.rows.append(self.rows[0])
        self.description = description() if metadata is None else metadata
        self.fault, self.action = fault, action or (lambda _stage: None)
        self.sfqid = None
        self.calls = []
        self.closed = 0

    def execute(self, sql, *, timeout):
        self.calls.append((sql, timeout))
        self.action("execute")
        if self.fault == "execute":
            raise RuntimeError(SECRET)
        self.sfqid = QUERY_ONE
        return self

    def fetchone(self):
        self.action("fetch")
        if self.fault == "fetch" or self.fault == "final_fetch" and not self.rows:
            raise RuntimeError(SECRET)
        return self.rows.pop(0) if self.rows else None

    def close(self):
        self.closed += 1
        self.action("close")
        if self.fault == "close":
            raise RuntimeError(SECRET)


class Connection:
    network_timeout = 1
    session_id = "PYTHON_ATTRIBUTE_IS_NOT_SQL_IDENTITY"

    def __init__(self, cursor=None, *, action=None):
        self.result = cursor or Cursor()
        self.action = action or (lambda _stage: None)
        self.opened = 0

    def cursor(self):
        self.opened += 1
        self.action("cursor")
        return self.result


def observe(connection=None, **kwargs):
    return identity.observe_destination_identity(connection or Connection(), scope(),
                                                 expected=expectation(), check_current=lambda: True, **kwargs)


def encoded(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def test_actual_sql_values_are_retained_typed_and_replayable():
    cursor = Cursor(row=("LOCATOR", "ORG", "ACCOUNT", "READER", "READ_WH", "DEST", "TEST", Decimal("42"), "SERVICE"))
    result = observe(Connection(cursor))
    assert tuple(result.values) == FIELDS
    assert result.values["session"] == Decimal("42")
    assert type(result.values["session"]) is Decimal
    assert result.query_id == QUERY_ONE
    assert result.scope_sha256 == scope().sha256
    assert result.sha256 == hashlib.sha256(result.original_bytes).hexdigest()
    assert result.byte_count == len(result.original_bytes) <= 65536
    document = json.loads(result.original_bytes)
    assert document["values"][7] == {"type": "decimal", "value": "42"}
    assert document["sql"] == identity.IDENTITY_SQL
    assert document["destination"] == {"database": "DEST", "schema": "TEST",
                                         "history_table": "ITEMS_HISTORY", "mirror_table": "ITEMS_MIRROR"}
    assert document["native_admission"] == "not_evaluated"
    assert document["transaction_consistency_verified"] is False
    assert document["origin"] == "dbapi_typed_result_not_wire_bytes"
    replay = identity.read_destination_identity(result.original_bytes, scope(), expected=expectation())
    assert replay.original_bytes == result.original_bytes and replay.values == result.values
    assert cursor.closed == 1 and cursor.rows == []
    assert cursor.calls == [(identity.IDENTITY_SQL, cursor.calls[0][1])]
    assert type(cursor.calls[0][1]) is int and 1 <= cursor.calls[0][1] <= 60
    assert identity.IDENTITY_SQL.startswith("SELECT CURRENT_ACCOUNT()")
    assert cursor.calls[0][0] == (
        'SELECT CURRENT_ACCOUNT() AS "account_locator", CURRENT_ORGANIZATION_NAME() AS "organization_name", '
        'CURRENT_ACCOUNT_NAME() AS "account_name", CURRENT_ROLE() AS "role", CURRENT_WAREHOUSE() AS "warehouse", '
        'CURRENT_DATABASE() AS "database", CURRENT_SCHEMA() AS "schema", CURRENT_SESSION() AS "session", '
        'CURRENT_USER() AS "user"')
    with pytest.raises(TypeError):
        result.values["role"] = "OTHER"
    with pytest.raises(FrozenInstanceError):
        result.query_id = QUERY_TWO


@pytest.mark.parametrize("field", [name for name in FIELDS if name != "session"])
def test_every_expected_effective_identity_is_required(field):
    row = list(Cursor().rows[0])
    row[FIELDS.index(field)] = "FOREIGN"
    connection = Connection(Cursor(row=tuple(row)))
    with pytest.raises(identity.DestinationIdentityError, match="^destination_identity_failed$"):
        observe(connection)
    assert connection.result.closed == 1


@pytest.mark.parametrize("field", [name for name in FIELDS if name != "session"])
@pytest.mark.parametrize("value", [None, "", "x" * 257, "line\nvalue", 42, True])
def test_core_values_are_nonnull_bounded_text(field, value):
    row = list(Cursor().rows[0])
    row[FIELDS.index(field)] = value
    with pytest.raises(identity.DestinationIdentityError):
        observe(Connection(Cursor(row=tuple(row))))


@pytest.mark.parametrize("value", [None, True, 42.0, "42", 0, -1, 10 ** 38,
                                   Decimal("NaN"), Decimal("Infinity"), Decimal("1.2")])
def test_session_requires_exact_positive_numeric_identity(value):
    row = list(Cursor().rows[0])
    row[7] = value
    with pytest.raises(identity.DestinationIdentityError):
        observe(Connection(Cursor(row=tuple(row))))


@pytest.mark.parametrize("case", ["missing", "extra", "partial", "order", "name", "type", "bool_type", "scale", "shape"])
def test_exact_metadata_and_single_complete_row(case):
    cursor = Cursor()
    if case == "missing":
        cursor.rows = []
    elif case == "extra":
        cursor.rows *= 2
    elif case == "partial":
        cursor.rows[0] = cursor.rows[0][:-1]
    elif case == "order":
        cursor.description = list(reversed(cursor.description))
    elif case == "shape":
        cursor.description[0] = cursor.description[0][:-1]
    else:
        index = 7 if case == "scale" else 0
        entry = list(cursor.description[index])
        entry[{"name": 0, "type": 1, "bool_type": 1, "scale": 5}[case]] = {
            "name": "foreign", "type": 0, "bool_type": True, "scale": 1}[case]
        cursor.description[index] = tuple(entry)
    with pytest.raises(identity.DestinationIdentityError):
        observe(Connection(cursor))
    assert cursor.closed == 1


@pytest.mark.parametrize("fault", ["execute", "fetch", "final_fetch", "close"])
def test_failures_are_expurgated_and_cursor_always_closes(fault):
    cursor = Cursor(fault=fault)
    with pytest.raises(identity.DestinationIdentityError) as error:
        observe(Connection(cursor))
    assert str(error.value) == "destination_identity_failed"
    assert error.value.__suppress_context__ and SECRET not in str(error.value)
    assert cursor.closed == 1


@pytest.mark.parametrize("stage", ["cursor", "execute", "fetch", "close"])
def test_current_fence_is_strict_and_checked_after_each_io(stage):
    current = [True]
    def action(at):
        if at == stage:
            current[0] = False
    cursor = Cursor(action=action)
    connection = Connection(cursor, action=action)
    with pytest.raises(identity.DestinationIdentityError):
        identity.observe_destination_identity(connection, scope(), expected=expectation(),
                                              check_current=lambda: current[0])
    assert cursor.closed == 1


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_invalid_initial_fence_prevents_cursor_creation(value):
    connection = Connection()
    with pytest.raises(identity.DestinationIdentityError):
        identity.observe_destination_identity(connection, scope(), expected=expectation(), check_current=lambda: value)
    assert connection.opened == 0


@pytest.mark.parametrize("stage", ["cursor", "execute", "fetch", "close"])
def test_deadline_rejects_late_success_even_at_close(monkeypatch, stage):
    clock = [1.0]
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
    def action(at):
        if at == stage:
            clock[0] += 61
    cursor = Cursor(action=action)
    with pytest.raises(identity.DestinationIdentityError):
        observe(Connection(cursor, action=action))
    assert cursor.closed == 1


@pytest.mark.parametrize("timeout", [None, 0, -1, True, 1.0, 61])
def test_effective_network_timeout_must_fit_remaining_budget(timeout):
    connection = Connection()
    connection.network_timeout = timeout
    with pytest.raises(identity.DestinationIdentityError):
        observe(connection)
    assert connection.opened == 0


@pytest.mark.parametrize("seconds", [0, -1, True, 1.0, 61])
def test_time_budget_is_strict_and_capped(seconds):
    with pytest.raises(identity.DestinationIdentityError):
        observe(max_seconds=seconds)


def test_missing_expectation_and_wrong_scope_fail_before_query():
    connection = Connection()
    for expected, s in [(None, scope()), (expectation(database="FOREIGN"), scope()),
                        (expectation(schema="FOREIGN"), scope())]:
        with pytest.raises(identity.DestinationIdentityError):
            identity.observe_destination_identity(connection, s, expected=expected, check_current=lambda: True)
    assert connection.opened == 0


def test_pair_requires_same_session_and_effective_identity_with_originals():
    before, after = observe(), observe()
    identity.validate_destination_identity_pair(before, after, scope=scope(), expected=expectation())
    row = list(Cursor().rows[0])
    row[7] = 43
    other = observe(Connection(Cursor(row=tuple(row))))
    with pytest.raises(identity.DestinationIdentityError):
        identity.validate_destination_identity_pair(before, other, scope=scope(), expected=expectation())
    with pytest.raises(identity.DestinationIdentityError):
        identity.validate_destination_identity_pair(before, after, scope=replace(scope(), mirror_table="OTHER"),
                                                   expected=expectation())


@pytest.mark.parametrize("case", ["sql", "scope", "table", "expected", "verdict", "time", "type", "extra"])
def test_replay_rejects_altered_contract_fields(case):
    document = json.loads(observe().original_bytes)
    if case == "sql":
        document["sql"] += " WHERE 1=1"
    elif case == "scope":
        document["scope_sha256"] = "f" * 64
    elif case == "table":
        document["destination"]["history_table"] = "FOREIGN"
    elif case == "expected":
        document["expectation_sha256"] = "f" * 64
    elif case == "verdict":
        document["native_admission"] = "verified"
    elif case == "time":
        document["completed_at"] = "2020-01-01T00:00:00"
    elif case == "type":
        document["values"][7] = {"type": "float", "value": "42.0"}
    else:
        document["extra"] = True
    with pytest.raises(identity.DestinationIdentityError):
        identity.read_destination_identity(encoded(document), scope(), expected=expectation())


@pytest.mark.parametrize("content", [b"{}", b'{"sql":1,"sql":2}', b'{"sql":NaN}', b'{"sql":Infinity}',
                                      b"\xff", b"{} trailing", b" " * 65537])
def test_replay_is_bounded_strict_json(content):
    with pytest.raises(identity.DestinationIdentityError):
        identity.read_destination_identity(content, scope(), expected=expectation())


def test_replay_rejects_duplicate_keys_in_an_otherwise_valid_original():
    content = observe().original_bytes
    pair = b'"native_admission":"not_evaluated"'
    content = content.replace(pair, pair + b"," + pair, 1)
    with pytest.raises(identity.DestinationIdentityError):
        identity.read_destination_identity(content, scope(), expected=expectation())


def test_query_id_is_optional_and_never_reattributes_an_old_value():
    cursor = Cursor()
    cursor.sfqid = QUERY_ONE
    assert observe(Connection(cursor)).query_id is None
    class Unavailable(Cursor):
        @property
        def sfqid(self):
            raise RuntimeError(SECRET)
        @sfqid.setter
        def sfqid(self, _value):
            pass
    assert observe(Connection(Unavailable())).query_id is None


def test_expectation_requires_all_bounded_server_owned_strings():
    for value in (None, "", " leading", "x" * 257, "bad\x00value", True):
        with pytest.raises(identity.DestinationIdentityError):
            expectation(role=value)


@pytest.mark.parametrize("time_after_callback", [61.0, -1.0, float("nan")])
def test_budget_rechecks_time_after_an_external_fence_callback(monkeypatch, time_after_callback):
    clock = [0.0]
    slow = [False]
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
    def current():
        if slow[0]:
            clock[0] = time_after_callback
        return True
    budget = identity._Budget(Connection(), current, 60)
    slow[0] = True
    with pytest.raises(identity.DestinationIdentityError):
        budget.check()


def test_replay_preserves_exact_bytes_and_decimal_representation():
    row = list(Cursor().rows[0])
    row[7] = Decimal("4.2E+1")
    observation = observe(Connection(Cursor(row=tuple(row))))
    content = json.dumps(json.loads(observation.original_bytes), indent=2).encode()
    replay = identity.read_destination_identity(content, scope(), expected=expectation())
    assert replay.original_bytes == content
    assert str(replay.values["session"]) == "42"
    assert replay.sha256 == hashlib.sha256(content).hexdigest()


def test_pair_revalidates_originals_and_public_projection():
    before, after = observe(), observe()
    changed = replace(after, query_id=QUERY_TWO)
    with pytest.raises(identity.DestinationIdentityError):
        identity.validate_destination_identity_pair(before, changed, scope=scope(), expected=expectation())
    with pytest.raises(identity.DestinationIdentityError):
        identity.validate_destination_identity_pair(before, after, scope=scope(), expected=expectation(role="OTHER"))


def test_optional_query_id_requires_a_readable_baseline():
    class Intermittent(Cursor):
        def __init__(self):
            self.reads = 0
            super().__init__()
        @property
        def sfqid(self):
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError(SECRET)
            return QUERY_ONE
        @sfqid.setter
        def sfqid(self, _value):
            pass
    assert observe(Connection(Intermittent())).query_id is None


@pytest.mark.parametrize("field,value", [("started_at", "2099-01-01T00:00:00Z"),
                                        ("started_at", "2020-01-01T00:00:00+01:00"),
                                        ("query_id", SECRET)])
def test_replay_rejects_bad_time_order_zone_and_unbounded_trace(field, value):
    document = json.loads(observe().original_bytes)
    document[field] = value
    with pytest.raises(identity.DestinationIdentityError):
        identity.read_destination_identity(encoded(document), scope(), expected=expectation())
