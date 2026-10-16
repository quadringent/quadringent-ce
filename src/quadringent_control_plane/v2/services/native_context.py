"""Semantic generations and opt-in durable native-context fences."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import uuid

from sqlalchemy import select, update

from .. import schema

_FIELDS = {
    "sources": {"ibmi_host", "ibmi_user", "tls_trust", "tls_pinned_pem", "detected_timezone", "paused_at"},
    "tables": {"key_strategy", "key_columns", "journal_library", "journal_name", "journal_status",
               "images", "discovered_columns", "schema_name", "table_name"},
    "destinations": {"snowflake_account", "destination_database", "destination_schema",
                     "service_user", "service_role", "paused_at"},
    "organizations": {"paused_at", "native_context_enforced"},
}
_ACTIVE = ContextVar("native_context_operation", default=None)


class NativeContextConflict(RuntimeError):
    """A context, operation or controller fence no longer belongs to this caller."""


@contextmanager
def context_transaction(engine, org_id):
    """Hold no provider or collection call inside this transaction."""
    with engine.connect() as connection:
        try:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA busy_timeout=1000")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            elif engine.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '1000ms'")
            else:
                raise NativeContextConflict("unsupported_context_store")
            statement = select(schema.organizations.c.id).order_by(schema.organizations.c.id)
            if org_id is not None:
                statement = statement.where(schema.organizations.c.id == org_id)
            rows = connection.execute(statement.with_for_update()).all()
            if org_id is not None and not rows:
                raise LookupError("organization_not_found")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def pipeline_rows(connection, *, pipeline_ids=None, source_id=None, destination_id=None, table_id=None):
    p, t, s, d = schema.pipelines, schema.tables, schema.sources, schema.destinations
    statement = select(p, s.c.org_id, s.c.id.label("source_id")).select_from(
        p.join(t, p.c.table_id == t.c.id).join(s, t.c.source_id == s.c.id).join(d, p.c.destination_id == d.c.id)
    ).where(s.c.org_id == d.c.org_id)
    for condition in (
        p.c.id.in_(pipeline_ids) if pipeline_ids is not None else None,
        t.c.source_id == source_id if source_id is not None else None,
        p.c.destination_id == destination_id if destination_id is not None else None,
        t.c.id == table_id if table_id is not None else None,
    ):
        if condition is not None:
            statement = statement.where(condition)
    return connection.execute(statement).mappings().all()


def org_for_pipeline(engine, pipeline_id):
    with engine.connect() as c:
        rows = pipeline_rows(c, pipeline_ids=(pipeline_id,))
    if len(rows) != 1:
        raise NativeContextConflict("pipeline_not_found")
    return rows[0]["org_id"]


def semantic_update(connection, table, resource_id, values):
    """Invalidate only semantic changes, atomically with the SQL update."""
    before = connection.execute(select(table).where(table.c.id == resource_id)).mappings().one()
    changed = any(
        (bool(before[name]) != bool(value) if name == "paused_at" else before[name] != value)
        for name, value in values.items() if name in _FIELDS.get(table.name, ())
    )
    if changed:
        if table.name == "sources":
            rows = pipeline_rows(connection, source_id=resource_id)
        elif table.name == "tables":
            rows = pipeline_rows(connection, table_id=resource_id)
        elif table.name == "destinations":
            rows = pipeline_rows(connection, destination_id=resource_id)
        else:
            rows = [r for r in pipeline_rows(connection) if r["org_id"] == resource_id]
        ids = [r["id"] for r in rows]
        if ids:
            connection.execute(update(schema.pipelines).where(schema.pipelines.c.id.in_(ids)).values(
                native_context_generation=schema.pipelines.c.native_context_generation + 1))
    return connection.execute(update(table).where(table.c.id == resource_id).values(**values))


@dataclass
class Operation:
    engine: object
    org_id: str
    token: str
    generations: dict = field(default_factory=dict)
    original_runs: dict = field(default_factory=dict)
    unresolved: bool = False
    enforced: bool | None = None


def _enforced(connection, org_id):
    return connection.execute(select(schema.organizations.c.native_context_enforced).where(
        schema.organizations.c.id == org_id)).scalar_one()


def _reserve(operation, pipeline_ids):
    reserved = {}
    runs = {}
    with context_transaction(operation.engine, operation.org_id) as c:
        enforced = _enforced(c, operation.org_id)
        if operation.enforced is not None and operation.enforced != enforced:
            raise NativeContextConflict("operation_fence_lost")
        rows = pipeline_rows(c, pipeline_ids=pipeline_ids)
        if len(rows) != len(set(pipeline_ids)) or any(r["org_id"] != operation.org_id for r in rows):
            raise NativeContextConflict("operation_scope_changed")
        for row in rows:
            if row["id"] in operation.generations:
                if (enforced and row["native_context_operation_id"] != operation.token
                        or row["native_context_generation"] != operation.generations[row["id"]]):
                    raise NativeContextConflict("operation_fence_lost")
                continue
            if enforced and row["native_context_operation_id"] is not None:
                raise NativeContextConflict("operation_pending")
            generation = row["native_context_generation"] + 1
            values = {"native_context_generation": generation}
            if enforced:
                values["native_context_operation_id"] = operation.token
            # Disabled tracking neither creates nor erases a strict pending effect.
            c.execute(update(schema.pipelines).where(schema.pipelines.c.id == row["id"]).values(**values))
            reserved[row["id"]] = generation
            runs[row["id"]] = row["active_run_id"]
    operation.generations.update(reserved)
    operation.original_runs.update(runs)
    operation.enforced = enforced


def require_operation(connection, operation, pipeline_id):
    row = connection.execute(select(schema.pipelines).where(schema.pipelines.c.id == pipeline_id)).mappings().one()
    if (_enforced(connection, operation.org_id) != operation.enforced
            or operation.enforced and row["native_context_operation_id"] != operation.token
            or row["native_context_generation"] != operation.generations.get(pipeline_id)):
        raise NativeContextConflict("operation_fence_lost")


def check_operation(engine, pipeline_id):
    """Check before external effects, including direct internal entrypoints."""
    operation = _ACTIVE.get()
    if operation is None or operation.engine is not engine:
        raise NativeContextConflict("operation_required")
    with context_transaction(engine, operation.org_id) as c:
        require_operation(c, operation, pipeline_id)


def operation_update(engine, pipeline_id, values, *, expected=None, original_run=False):
    operation = _ACTIVE.get()
    if operation is None or operation.engine is not engine:
        raise NativeContextConflict("operation_required")
    with context_transaction(engine, operation.org_id) as c:
        require_operation(c, operation, pipeline_id)
        statement = update(schema.pipelines).where(schema.pipelines.c.id == pipeline_id)
        if original_run:
            statement = statement.where(schema.pipelines.c.active_run_id == operation.original_runs[pipeline_id])
        for name, value in (expected or {}).items():
            statement = statement.where(schema.pipelines.c[name] == value)
        if c.execute(statement.values(**values)).rowcount != 1:
            raise NativeContextConflict("operation_context_changed")


def mark_operation_unresolved():
    operation = _ACTIVE.get()
    if operation is None:
        raise NativeContextConflict("operation_required")
    operation.unresolved = True


@contextmanager
def external_operation(engine, pipeline_ids):
    pipeline_ids = tuple(pipeline_ids)
    if not pipeline_ids:
        yield None
        return
    org_id = org_for_pipeline(engine, pipeline_ids[0])
    operation = _ACTIVE.get()
    nested = operation is not None
    if nested and (operation.engine is not engine or operation.org_id != org_id):
        raise NativeContextConflict("nested_operation_scope")
    if not nested:
        operation = Operation(engine, org_id, uuid.uuid4().hex)
    _reserve(operation, pipeline_ids)
    token = _ACTIVE.set(operation)
    try:
        yield operation
        if not nested and not operation.unresolved:
            with context_transaction(engine, org_id) as c:
                for pipeline_id in operation.generations:
                    require_operation(c, operation, pipeline_id)
                if operation.enforced:
                    c.execute(update(schema.pipelines).where(
                        schema.pipelines.c.id.in_(operation.generations),
                        schema.pipelines.c.native_context_operation_id == operation.token,
                    ).values(native_context_operation_id=None))
    except BaseException:
        operation.unresolved = True
        raise
    finally:
        _ACTIVE.reset(token)


def pipeline_effect(function):
    """Reserve direct executor calls and reuse a service's outer operation."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        pipeline_id = kwargs.get("pipeline_id", args[0] if args else None)
        if kwargs.get("event") == "replay":
            return function(self, *args, **kwargs)
        try:
            with external_operation(self._engine, (pipeline_id,)):
                return function(self, *args, **kwargs)
        except NativeContextConflict as error:
            from ..executor.kubernetes import ExecutorError

            code = "not_found" if str(error) == "pipeline_not_found" else "capability_unavailable"
            raise ExecutorError(code, "Native context operation requires observation or a new fence") from error
    return wrapped


def workload_pipelines(engine, *, source_id=None, destination_id=None, journal=None):
    with engine.connect() as c:
        rows = pipeline_rows(c, source_id=source_id, destination_id=destination_id)
        if journal is not None:
            table_ids = c.execute(select(schema.tables.c.id).where(
                schema.tables.c.journal_library == journal[0], schema.tables.c.journal_name == journal[1])).scalars().all()
            rows = [row for row in rows if row["table_id"] in table_ids]
    return tuple(row["id"] for row in rows)


def canonical(value, *, maximum=262144):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded.encode()) > maximum:
        raise ValueError("native_manifest_budget")
    return encoded


def context_snapshot(connection, org_id, pipeline_id, source_restriction=()):
    p, t, s, d, o = schema.pipelines, schema.tables, schema.sources, schema.destinations, schema.organizations
    statement = select(p, t.c.source_id).select_from(
        p.join(t, p.c.table_id == t.c.id).join(s, t.c.source_id == s.c.id).join(d, p.c.destination_id == d.c.id)
    ).where(p.c.id == pipeline_id, s.c.org_id == org_id, d.c.org_id == org_id)
    row = connection.execute(statement).mappings().first()
    if row is None or source_restriction and row["source_id"] not in source_restriction:
        raise LookupError("pipeline_not_found")
    context = {"org_id": org_id, "pipeline_id": row["id"], "table_id": row["table_id"],
               "source_id": row["source_id"], "destination_id": row["destination_id"],
               "copy_run_id": row["active_run_id"], "generation": row["native_context_generation"]}
    for name, table, identifier, fields in (
        ("source", s, row["source_id"], sorted(_FIELDS["sources"] - {"tls_pinned_pem"})),
        ("table", t, row["table_id"], sorted(_FIELDS["tables"])),
        ("destination", d, row["destination_id"], sorted(_FIELDS["destinations"])),
        ("organization", o, org_id, sorted(_FIELDS["organizations"])),
    ):
        record = connection.execute(select(table).where(table.c.id == identifier)).mappings().one()
        context[name] = {f: bool(record[f]) if f == "paused_at" else record[f] for f in fields}
        if name == "source":
            context[name]["tls_pin_sha256"] = hashlib.sha256((record["tls_pinned_pem"] or "").encode()).hexdigest()
    text = canonical(context)
    return row, context, text, hashlib.sha256(text.encode()).hexdigest()



@dataclass(frozen=True)
class OperationObservation:
    operation_id: str
    contexts_sha256: str
    artifact: object


def resolve_operation(engine, *, org_id, token, observer=None):
    """An internal observer must read actual state outside the SQL fence."""
    if observer is None:
        raise NativeContextConflict("operation_observation_unavailable")

    def snapshot(connection):
        rows = [r for r in pipeline_rows(connection) if r["org_id"] == org_id
                and r["native_context_operation_id"] == token]
        if not rows:
            raise NativeContextConflict("operation_not_pending")
        contexts = {r["id"]: context_snapshot(connection, org_id, r["id"])[1] for r in rows}
        return contexts, hashlib.sha256(canonical(contexts).encode()).hexdigest()

    with context_transaction(engine, org_id) as c:
        contexts, digest = snapshot(c)
    observed = observer.observe(token, contexts)
    from .native_verifications import NativeArtifact

    if (type(observed) is not OperationObservation or observed.operation_id != token
            or observed.contexts_sha256 != digest or type(observed.artifact) is not NativeArtifact):
        raise NativeContextConflict("operation_observation_invalid")
    record = canonical(dict(operation_id=token, contexts_sha256=digest,
                            artifact=observed.artifact.to_dict()), maximum=8192)
    with context_transaction(engine, org_id) as c:
        _contexts, current = snapshot(c)
        if current != digest:
            raise NativeContextConflict("operation_observation_superseded")
        result = c.execute(update(schema.pipelines).where(
            schema.pipelines.c.id.in_(contexts), schema.pipelines.c.native_context_operation_id == token,
        ).values(native_context_operation_id=None, native_context_observation_json=record))
        if result.rowcount != len(contexts):
            raise NativeContextConflict("operation_observation_fence_lost")
