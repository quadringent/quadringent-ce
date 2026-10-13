"""Concurrent startup checks on isolated PostgreSQL installation databases."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import make_url

from quadringent_control_plane.v2 import db, entrypoint, schema

pytestmark = pytest.mark.postgres


@pytest.fixture
def isolated_organization_postgres(postgres_dsn):
    database = "organization_startup_" + uuid4().hex
    administrative = db.create_engine_for(postgres_dsn)
    dsn = make_url(postgres_dsn).set(database=database).render_as_string(hide_password=False)
    created = False
    try:
        with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        created = True
        db.run_migrations_locked(dsn)
        yield dsn
    finally:
        if created:
            with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
        administrative.dispose()


def _start(engine, org_id):
    try:
        entrypoint.ensure_organization(engine, org_id)
    except entrypoint.ConfigurationError:
        return "refused"
    return "accepted"


def _wait_for_both_sessions_blocked(observer, names):
    statement = text(
        "SELECT count(DISTINCT activity.pid) "
        "FROM pg_stat_activity AS activity "
        "JOIN pg_locks AS locks ON locks.pid = activity.pid "
        "WHERE activity.datname = current_database() "
        "AND activity.application_name IN :names AND NOT locks.granted"
    ).bindparams(bindparam("names", expanding=True))
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with observer.connect() as connection:
            if connection.execute(statement, {"names": names}).scalar_one() == 2:
                return
        sleep(0.02)
    raise AssertionError("Both isolated startup sessions must wait behind the witness transaction")


@pytest.mark.parametrize("org_ids", [("same", "same"), ("first", "second")])
def test_postgres_concurrent_startups_enforce_one_organization(
    isolated_organization_postgres, org_ids
):
    names = ["startup_" + uuid4().hex for _org_id in org_ids]
    engines = [
        db.create_engine_for(
            make_url(isolated_organization_postgres)
            .update_query_dict(
                {
                    "application_name": name,
                    "connect_timeout": "5",
                    "options": "-c lock_timeout=10000 -c statement_timeout=15000",
                }
            )
            .render_as_string(hide_password=False)
        )
        for name in names
    ]
    observer = db.create_engine_for(isolated_organization_postgres)
    try:
        with observer.connect() as witness:
            transaction = witness.begin()
            witness.execute(text("LOCK TABLE organizations IN SHARE ROW EXCLUSIVE MODE"))
            # A witness lock forces both attempts to start from the same empty database.
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(_start, engine, org_id)
                    for engine, org_id in zip(engines, org_ids, strict=True)
                ]
                try:
                    _wait_for_both_sessions_blocked(observer, names)
                    assert all(not future.done() for future in futures)
                finally:
                    transaction.rollback()
                outcomes = [future.result(timeout=20) for future in futures]
        expected = ["accepted", "accepted"] if org_ids[0] == org_ids[1] else ["accepted", "refused"]
        assert sorted(outcomes) == sorted(expected)
        with observer.connect() as connection:
            stored_ids = connection.execute(select(schema.organizations.c.id)).scalars().all()
        assert len(stored_ids) == 1
        assert stored_ids[0] in org_ids
    finally:
        for engine in engines:
            engine.dispose()
        observer.dispose()
