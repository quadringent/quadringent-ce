"""The host plans feed the real in-image entry points up to their first network call.

The env values the host writes are given to the real argument parser of ``local_runtime``, to the real capture
script (up to the worker construction) and to the real loader script (up to the Snowflake connection), on a real
local backend admitted by ``local_runtime.run_admission``. No Docker, no IBM i, no Snowflake.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
import os
import sys

import pytest

from local_cli_support import Cli
from quadringent.continuous import ReceiverSnapshot
from quadringent.local import dockerplan as plan
from quadringent.local import tableset
from quadringent.local.discovery import declared_columns
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.storage_backend import StorageBackend
from quadringent.table_discovery import DiscoveredColumn

runtime = pytest.importorskip("quadringent_control_plane.v2.executor.local_runtime")
from quadringent_control_plane.v2.executor.boundary import JournalBoundary  # noqa: E402

IDENTITY = ReceiverIdentity("QDCLIB", "QSQJRN", "QDCLIB", "QSQJRN0001", "native-test-v1", "generation-a")
IMAGE_ENV = {"AS400_JAVA": "java", "AS400_JAVA_CLASSPATH": "/app/probe.jar:/app/lib/*"}
COLUMNS = declared_columns((DiscoveredColumn("ID", "DECIMAL", 9, 0, False), DiscoveredColumn("NAME", "VARCHAR", 40, None, True)))


class FirstNetworkCall(Exception):
    pass


@pytest.fixture()
def instance(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init() == 0
    config = cli.config()
    data_env = {"QUADRINGENT_STORAGE_BACKEND": "local", "AS400_RAW_BUCKET": config.label,
                "AS400_LOCAL_DATA_DIR": str(cli.paths.data)}
    storage = StorageBackend.from_environment(data_env)
    boundary = JournalBoundary("QDCLIB", "QSQJRN0001", 4200, datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc),
                               receiver_identity=IDENTITY)
    admission = runtime.run_admission(
        storage, boundary, source_id=config.source_id, pipeline_id=config.pipeline_id, table_id=config.table_id,
        journal_library="QDCLIB", journal_name="QSQJRN", raw_prefix_root="raw")
    tableset.parse_admission(admission, table_id=config.table_id, pipeline_id=config.pipeline_id)
    return cli, config, admission


def process_environment(monkeypatch, env, data_dir):
    for name in list(os.environ):
        if name.startswith(("AS400_", "QUADRINGENT_", "ISERIES_", "SNOWFLAKE_")):
            monkeypatch.delenv(name)
    for key, value in {**IMAGE_ENV, **env, "AS400_LOCAL_DATA_DIR": str(data_dir)}.items():
        monkeypatch.setenv(key, value)


def runtime_arguments(spec):
    """Parse the container command with the real parser of local_runtime."""
    assert spec.command[:3] == ("-P", "-m", plan.RUNTIME_MODULE)
    return runtime.build_parser().parse_args(list(spec.command[3:]))


def test_every_container_command_is_accepted_by_the_real_runtime_parser(instance):
    cli, config, admission = instance
    paths = cli.paths
    journal = ("QDCLIB", "QSQJRN")
    specs = {
        "fs-probe": plan.fs_probe_spec(config, paths),
        "boundary": plan.boundary_spec(config, paths, time_zone="Europe/Paris", journal=journal),
        "admit": plan.admit_spec(config, paths, boundary_json='{"a":1}', journal=journal),
        "copy": plan.copy_spec(config, paths, time_zone="Europe/Paris", journal=journal),
        "capture": plan.capture_spec(config, paths, time_zone="Europe/Paris", capture_env={}),
        "loader": plan.loader_spec(config, paths, table_set_json="[]"),
        "verify": plan.verify_spec(config, paths),
        "probe": plan.probe_spec(config, paths, port=9476),
        "discover": plan.discover_spec(config, paths),
    }
    commands = {role: runtime_arguments(spec).command for role, spec in specs.items()}
    assert commands == {"fs-probe": "fs-probe", "boundary": "boundary", "admit": "admit", "copy": "copy",
                        "capture": "exec-capture", "loader": "exec-loader", "verify": "snowflake-verify",
                        "probe": "exec-capture", "discover": "exec-capture"}
    admit = runtime_arguments(specs["admit"])
    assert (admit.source_id, admit.pipeline_id, admit.table_id, admit.raw_prefix_root) == (
        config.source_id, config.pipeline_id, config.table_id, "raw")
    assert runtime_arguments(specs["copy"]).admission_file == plan.ADMISSION_TARGET
    capture = runtime_arguments(specs["capture"])
    assert runtime._child_arguments(capture.child_arguments) == ["--console-snapshot-path", plan.CONSOLE_TARGET]
    probe = runtime_arguments(specs["probe"])
    assert probe.script == "/app/quadringent_source_probe_job.py"
    assert runtime._child_arguments(probe.child_arguments) == ["--host", config.ibmi.host, "--user", config.ibmi.user,
                                                               "--port", "9476"]


def test_the_exec_wrapper_hands_the_password_to_the_child_environment_only(instance, tmp_path, monkeypatch):
    cli, config, _ = instance
    spec = plan.capture_spec(config, cli.paths, time_zone="Europe/Paris", capture_env={})
    monkeypatch.setenv(runtime.ENV_IBMI_PASSWORD_FILE, str(cli.paths.ibmi_password))
    seen = {}
    args = runtime_arguments(spec)
    runtime.command_exec_capture(args, execve=lambda path, argv, env: seen.update(path=path, argv=argv, env=env))
    password = cli.paths.ibmi_password.read_text()
    assert seen["env"]["ISERIES_PASSWORD"] == password
    assert password not in " ".join(seen["argv"])
    assert seen["argv"][1:] == ["/app/as400_continuous_capture.py", "--console-snapshot-path", plan.CONSOLE_TARGET]


def test_the_boundary_container_reads_the_catalog_with_the_host_environment(instance):
    cli, config, _ = instance
    spec = plan.boundary_spec(config, cli.paths, time_zone="Europe/Paris", journal=("QDCLIB", "QSQJRN"))
    environ = {**IMAGE_ENV, **spec.env, runtime.ENV_IBMI_PASSWORD_FILE: str(cli.paths.ibmi_password)}
    seen = {}

    class Catalog:
        def snapshot(self, required_receiver=None):
            return [ReceiverSnapshot("QDCLIB", "QSQJRN0001", 1, 4200, "ATTACHED")]

        def identity(self, **kwargs):
            return IDENTITY

    def factory(**kwargs):
        seen.update(kwargs)
        return Catalog()

    boundary = runtime.read_boundary(environ, catalog_factory=factory)
    document = runtime.observed_boundary_to_dict(boundary)
    assert document["receiver_name"] == "QSQJRN0001" and document["last_sequence"] == 4200
    assert (seen["host"], seen["user"], seen["journal_library"], seen["journal_name"]) == (
        config.ibmi.host, config.ibmi.user, "QDCLIB", "QSQJRN")
    assert seen["password"] == cli.paths.ibmi_password.read_text() and seen["ca_file"] is None
    assert runtime_arguments(spec).command == "boundary"


def test_the_fs_probe_container_accepts_the_prepared_data_directory(instance, monkeypatch):
    cli, config, _ = instance
    spec = plan.fs_probe_spec(config, cli.paths)
    process_environment(monkeypatch, spec.env, cli.paths.data)
    result, code = runtime.command_fs_probe(runtime_arguments(spec))
    # the marker and layout written by init are what the host controls; the runner's file system decides the rest
    assert result["checks"]["marker"]["ok"] is True and result["checks"]["data_dir"]["ok"] is True
    assert code == (0 if result["ok"] else 1)


def test_the_capture_environment_resolves_in_the_real_script_up_to_the_first_worker(instance, monkeypatch, tmp_path):
    cli, config, admission = instance
    env = plan.capture_spec(
        config, cli.paths, time_zone="Europe/Paris",
        capture_env=tableset.capture_identity_env(admission, table=config.table_name, journal_library="QDCLIB",
                                                  journal_name="QSQJRN")).env
    process_environment(monkeypatch, {**env, "ISERIES_PASSWORD": "not-a-secret"}, cli.paths.data)
    capture = importlib.import_module("as400_continuous_capture")

    class Worker:
        def __init__(self, **kwargs):
            raise FirstNetworkCall

    monkeypatch.setattr(capture, "PersistentJavaWorker", Worker)
    monkeypatch.setattr(sys, "argv", ["as400_continuous_capture.py", "--console-snapshot-path", str(tmp_path / "c.json")])
    with pytest.raises(FirstNetworkCall):
        capture.main()
    position = capture._bootstrap_position()
    assert (position.receiver, position.sequence) == ("QSQJRN0001", 4200)


def test_a_capture_with_a_foreign_binding_is_refused_by_the_real_script(instance, monkeypatch, tmp_path):
    cli, config, admission = instance
    env = plan.capture_spec(
        config, cli.paths, time_zone="Europe/Paris",
        capture_env=tableset.capture_identity_env(admission, table=config.table_name, journal_library="QDCLIB",
                                                  journal_name="QSQJRN")).env
    process_environment(monkeypatch, {**env, "AS400_RAW_BUCKET": "qdt-ffffffff"}, cli.paths.data)
    capture = importlib.import_module("as400_continuous_capture")
    monkeypatch.setattr(sys, "argv", ["as400_continuous_capture.py", "--console-snapshot-path", str(tmp_path / "c.json")])
    with pytest.raises(ValueError):
        capture.main()


def test_the_loader_environment_resolves_in_the_real_script_up_to_the_snowflake_connection(instance, monkeypatch):
    cli, config, admission = instance
    entry = tableset.loader_table_entry(admission, schema=config.table_schema, table=config.table_name,
                                        key_columns=("ID",), columns=tuple(COLUMNS))
    env = plan.loader_spec(config, cli.paths, table_set_json=tableset.dumps([entry])).env
    process_environment(monkeypatch, {**env, "SNOWFLAKE_PRIVATE_KEY_PEM": "not-a-key"}, cli.paths.data)
    loader = importlib.import_module("quadringent_destination_loader")

    def connect(**kwargs):
        assert kwargs["account"] == config.snowflake.account and kwargs["role"] == config.snowflake.role
        assert loader._snowflake_role_to_warehouse(kwargs["role"]) == config.warehouse
        raise FirstNetworkCall

    monkeypatch.setattr(loader, "_connect_snowflake", connect)
    with pytest.raises(FirstNetworkCall):
        loader.main(["--once"])
    tables = loader.parse_table_set(env["QUADRINGENT_LOADER_TABLE_SET_JSON"], require_bound=True)
    plan_for = loader.build_plan(tables[0], database=env["QUADRINGENT_DESTINATION_DATABASE"],
                                 schema=env["QUADRINGENT_DESTINATION_SCHEMA"], mirror_schema=env["QUADRINGENT_MIRROR_SCHEMA"])
    assert plan_for.qualified_history_table.startswith('"QUADRINGENT"."RAW".')
    assert plan_for.qualified_mirror_table.startswith('"QUADRINGENT"."CURATED".')


def test_the_snowflake_verifier_reads_the_host_environment_and_the_key_file(instance, monkeypatch):
    cli, config, _ = instance
    spec = plan.verify_spec(config, cli.paths)
    process_environment(monkeypatch, {**spec.env, runtime.ENV_SNOWFLAKE_KEY_FILE: str(cli.paths.snowflake_key)},
                        cli.paths.data)
    seen = {}

    class Verifier:
        def verify(self, request):
            seen["request"] = request
            from quadringent_control_plane.v2.services.destination_verifier import (
                DestinationVerificationResult, VerificationOutcome)
            ok = VerificationOutcome(True, "ok")
            return DestinationVerificationResult(ok, ok, ok, ok, ok, ok)

    result, code = runtime.command_snowflake_verify(runtime_arguments(spec), verifier=Verifier())
    request = seen["request"]
    assert code == 0 and result["ok"] is True
    assert (request.snowflake_account, request.service_user, request.service_role, request.warehouse) == (
        config.snowflake.account, config.snowflake.user, config.snowflake.role, config.warehouse)
    assert request.private_key_pem == cli.paths.snowflake_key.read_text()
    assert "BEGIN PRIVATE KEY" not in json.dumps(result)
