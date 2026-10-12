"""The generated Snowflake setup script must allow what the loader and the
verifier actually do, and nothing more.

This is a local, static check of the generated SQL text: no statement is
executed on Snowflake. It derives the required operations from the same
code that uses them (executor schema defaults, verifier schemas, loader DDL)
instead of restating expected strings.
"""

from __future__ import annotations

import re

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.destination_verifier import (
    DECLARED_HISTORY_SCHEMA,
    DECLARED_MIRROR_SCHEMA,
    validated_destination_scope,
    warehouse_name_for,
)
from quadringent_control_plane.v2.services.destinations import (
    DestinationValidationError,
    DestinationsService,
)

GRANT = re.compile(r"^GRANT (?P<privileges>.+?) ON (?P<kind>WAREHOUSE|DATABASE|SCHEMA|ACCOUNT|ALL TABLES IN SCHEMA|FUTURE TABLES IN SCHEMA)"
                   r"(?: (?P<target>[A-Z0-9_$.]+))? TO ROLE (?P<role>[A-Z0-9_$]+);$")
GRANT_ROLE = re.compile(r"^GRANT ROLE (?P<role>[A-Z0-9_$]+) TO USER (?P<user>[A-Z0-9_$]+);$")
ADMIN_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN")


@pytest.fixture()
def service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'provisioning.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Synthetic org"})
    try:
        yield DestinationsService(engine, SecretBox(SecretBox.generate_key()), org_id="org1")
    finally:
        engine.dispose()


def grants_by_role(script: str) -> dict[str, set[tuple[str, str, str | None]]]:
    """Every privilege granted, per role, as (privilege, object kind, object)."""
    granted: dict[str, set[tuple[str, str, str | None]]] = {}
    for line in (raw.strip() for raw in script.splitlines()):
        if not line.startswith("GRANT ") or GRANT_ROLE.match(line):
            continue
        match = GRANT.match(line)
        assert match, f"unparsed grant: {line}"
        for privilege in (item.strip() for item in match["privileges"].split(",")):
            granted.setdefault(match["role"], set()).add((privilege, match["kind"], match["target"]))
    return granted


def required_service_operations(database: str, output_schema: str | None, *, mode: str, mirror: str):
    """What the runtime role does: the executor resolves the schemas, the
    loader creates the history and mirror tables in them (ensure_tables_exist)
    and the verifier proves CREATE TABLE on each declared schema."""
    history = output_schema or DECLARED_HISTORY_SCHEMA
    mirror_schema = output_schema or DECLARED_MIRROR_SCHEMA
    required = {("USAGE", "DATABASE", database)}
    for schema in {history, mirror_schema}:
        required |= {("USAGE", "SCHEMA", f"{database}.{schema}"), ("CREATE TABLE", "SCHEMA", f"{database}.{schema}")}
    if mode == "streaming" and mirror == "task_driven":
        required.add(("EXECUTE TASK", "ACCOUNT", None))
    return required


VARIANTS = [
    pytest.param({}, id="copy_merge-default-schemas"),
    pytest.param({"destination_mode": "streaming"}, id="streaming-loader-merge"),
    pytest.param({"destination_mode": "streaming", "mirror_option": "task_driven"}, id="streaming-task-driven"),
    pytest.param({"destination_database": "client_db"}, id="custom-database-default-schemas"),
    pytest.param({"destination_database": "client_db", "destination_schema": "site_a"}, id="single-custom-schema"),
    pytest.param({"destination_mode": "streaming", "destination_schema": "site_a"}, id="streaming-single-schema"),
    pytest.param({"destination_mode": "copy_merge", "mirror_option": "task_driven"}, id="task-driven-ignored-without-streaming"),
]


@pytest.mark.parametrize("options", VARIANTS)
def test_service_role_receives_every_privilege_the_runtime_needs(service, options):
    record, _ = service.create(snowflake_account="synthetic-account", **options)
    script = service.setup_script(record.id)
    database, output_schema = validated_destination_scope(
        options.get("destination_database", "QUADRINGENT"), options.get("destination_schema"))
    granted = grants_by_role(script)[record.service_role]
    required = required_service_operations(
        database, output_schema,
        mode=options.get("destination_mode", "copy_merge"), mirror=options.get("mirror_option", "loader_merge"))
    required.add(("USAGE", "WAREHOUSE", warehouse_name_for(record.service_role)))
    missing = required - granted
    assert not missing, f"privileges the loader or verifier need but the script does not grant: {missing}"


@pytest.mark.parametrize("options", VARIANTS)
def test_service_role_receives_nothing_beyond_the_runtime_needs(service, options):
    record, _ = service.create(snowflake_account="synthetic-account", **options)
    script = service.setup_script(record.id)
    database, output_schema = validated_destination_scope(
        options.get("destination_database", "QUADRINGENT"), options.get("destination_schema"))
    granted = grants_by_role(script)[record.service_role]
    allowed = required_service_operations(
        database, output_schema,
        mode=options.get("destination_mode", "copy_merge"), mirror=options.get("mirror_option", "loader_merge"))
    allowed.add(("USAGE", "WAREHOUSE", warehouse_name_for(record.service_role)))
    assert granted - allowed == set()


@pytest.mark.parametrize("options", VARIANTS)
def test_reader_role_is_read_only_on_the_mirror(service, options):
    record, _ = service.create(snowflake_account="synthetic-account", **options)
    script = service.setup_script(record.id)
    reader = record.service_role.replace("QDT_ROLE_", "QDT_READ_")
    privileges = {privilege for privilege, _, _ in grants_by_role(script)[reader]}
    assert privileges == {"USAGE", "SELECT"}


@pytest.mark.parametrize("options", VARIANTS)
def test_script_contains_no_secret_and_no_administrative_escalation(service, options):
    record, private_key = service.create(snowflake_account="synthetic-account", **options)
    script = service.setup_script(record.id)
    private_body = "".join(line for line in private_key.splitlines() if "PRIVATE KEY" not in line)
    assert "PRIVATE KEY" not in script and private_body[:64] not in script
    assert "PASSWORD" not in script.upper()
    statements = [line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("--")]
    for line in statements:
        upper = line.upper()
        assert not upper.startswith("USE ROLE")
        assert "OWNERSHIP" not in upper and "ALL PRIVILEGES" not in upper and "MANAGE GRANTS" not in upper
        if upper.startswith("GRANT ROLE"):
            assert not any(admin in upper for admin in ADMIN_ROLES)


@pytest.mark.parametrize("field,value", [
    ("snowflake_account", "acme; DROP DATABASE X"),
    ("snowflake_account", "acme' OR '1'='1"),
    ("snowflake_account", "acme\nGRANT ROLE ACCOUNTADMIN TO USER X"),
    ("destination_database", "QUADRINGENT; GRANT OWNERSHIP"),
    ("destination_database", "db name"),
    ("destination_schema", "RAW;--"),
    ("destination_schema", "s\"chema"),
    ("destination_schema", "RAW\nGRANT ROLE ACCOUNTADMIN TO ROLE X"),
])
def test_injection_attempts_are_rejected_before_any_script_exists(service, field, value):
    options = {"snowflake_account": "synthetic-account", field: value}
    with pytest.raises(DestinationValidationError):
        service.create(**options)
    assert service.list() == ()


@pytest.mark.parametrize("options", VARIANTS)
def test_reader_role_only_reaches_the_mirror_schema(service, options):
    record, _ = service.create(snowflake_account="synthetic-account", **options)
    database, output_schema = validated_destination_scope(
        options.get("destination_database", "QUADRINGENT"), options.get("destination_schema"))
    mirror = f"{database}.{output_schema or DECLARED_MIRROR_SCHEMA}"
    reader = record.service_role.replace("QDT_ROLE_", "QDT_READ_")
    targets = {target for _, kind, target in grants_by_role(service.setup_script(record.id))[reader] if kind != "DATABASE"}
    assert targets == {mirror}
