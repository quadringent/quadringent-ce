"""The environment of a local run feeds the real capture and loader configuration code.

Both scripts run unchanged. The capture reader is started against a source that
cannot be reached (``false`` stands in for the JVM), so it gets exactly as far as the
first source contact; the loader is stopped at the Snowflake connection. Everything
before that point (binding checks, local object/checkpoint/gate stores, scope
markers) is the production path on the local backend.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import Mock

import pytest

import quadringent_control_plane  # noqa: F401  (the package must win over scripts/quadringent_control_plane.py)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import as400_continuous_capture as capture  # noqa: E402
import quadringent_destination_loader as loader  # noqa: E402

from local_support import LABEL, local_env, make_data_dir  # noqa: E402
from quadringent.receiver_identity import ReceiverIdentity  # noqa: E402
from quadringent.storage_backend import StorageBackend  # noqa: E402
from quadringent_control_plane.v2.executor import local_runtime as runtime  # noqa: E402
from quadringent_control_plane.v2.executor.boundary import JournalBoundary  # noqa: E402
from quadringent_control_plane.v2.executor.manifests import LoaderTableSpec  # noqa: E402

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")
RAW_ROOT = "raw/example"


@pytest.fixture()
def admitted(tmp_path, monkeypatch):
    data = make_data_dir(tmp_path)
    env = local_env(data)
    storage = StorageBackend.from_environment(env)
    boundary = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY)
    admission = runtime.admit({
        "source_id": "src-1", "pipeline_id": "pipe-1", "table_id": "tbl-orders", "journal_library": "TESTLIB",
        "journal_name": "TESTJRN", "raw_prefix": RAW_ROOT, "boundary": runtime.observed_boundary_to_dict(boundary)},
        storage)
    return data, env, admission


def capture_environment(env, admission, **overrides):
    values = dict(env)
    values.update({
        "ISERIES_HOST": "192.0.2.10", "ISERIES_USER": "TESTUSER", "ISERIES_SCHEMA": "TESTLIB",
        "ISERIES_TABLE": "QDC_ORDERS", "AS400_STREAM_KEY": admission["stream_key"],
        "AS400_JOURNAL_LIBRARY": "TESTLIB", "AS400_JOURNAL_NAME": "TESTJRN",
        "AS400_JAVA": shutil.which("false") or "false", "AS400_JAVA_CLASSPATH": "offline.jar",
        "AS400_CAPTURE_BINDING_JSON": json.dumps(admission["binding"]),
        "AS400_RAW_PREFIX": f"{RAW_ROOT}/qdc_orders/journal", "AS400_CHECKPOINT_SCOPE": "source-raw-v1",
        "AS400_RECEIPTED_SCANS": "true", "AS400_SOURCE_TIME_ZONE": "Europe/Paris",
        "AS400_READER_TIMEOUT_SECONDS": "300", "ISERIES_PASSWORD": "offline-only",
    })
    values.update(overrides)
    return values


def run_capture(monkeypatch, tmp_path, values, *extra):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["as400_continuous_capture.py", "--max-polls", "1",
                                      "--console-snapshot-path", str(tmp_path / "console.json"), *extra])
    return capture.main()


def test_capture_reaches_the_first_source_contact_on_local_storage(admitted, monkeypatch, tmp_path):
    data, env, admission = admitted
    with pytest.raises(RuntimeError):  # the unreachable source, after every storage and binding check
        run_capture(monkeypatch, tmp_path, capture_environment(env, admission))
    gates = list((data / "state" / LABEL / "source-gates").glob("*.json"))
    assert len(gates) == 1, "the connect attempt went through the local source gate"
    assert (data / "objects" / LABEL / RAW_ROOT / "qdc_orders" / "journal").is_dir()


def test_capture_refuses_a_binding_that_is_not_the_durable_original(admitted, monkeypatch, tmp_path):
    _data, env, admission = admitted
    forged = json.loads(json.dumps(admission["binding"]))
    forged["capture_epoch"] = "forged-epoch"
    forged["bootstrap"]["capture_epoch"] = "forged-epoch"
    with pytest.raises(ValueError, match="durable original admission"):
        run_capture(monkeypatch, tmp_path, capture_environment(env, admission, AS400_CAPTURE_BINDING_JSON=json.dumps(forged)))


def test_capture_refuses_a_binding_of_another_authority(admitted, monkeypatch, tmp_path):
    _data, env, admission = admitted
    other = dict(env, AS400_RAW_BUCKET="qdt-other-store")
    with pytest.raises(ValueError, match="differs from its original capture binding"):
        run_capture(monkeypatch, tmp_path, capture_environment(other, admission))


@pytest.mark.parametrize("option", [["--reserve-run-id", "run-1"]])
def test_capture_keeps_the_aws_only_features_off_the_local_backend(admitted, monkeypatch, tmp_path, option):
    _data, env, admission = admitted
    with pytest.raises(ValueError, match="require the aws backend"):
        run_capture(monkeypatch, tmp_path, capture_environment(env, admission), *option)


def loader_environment(env, admission):
    table = LoaderTableSpec(
        table_id="tbl-orders", schema_name="TESTLIB", table_name="QDC_ORDERS", key_columns=("ID",),
        columns=({"name": "ID", "kind": "integer", "nullable": False},), evidence_key=admission["evidence_key"],
        pipeline_id="pipe-1", run_id=admission["run_id"],
        capture_binding=JournalBoundary.from_dict(admission["boundary"]).capture_binding)
    values = dict(env)
    values.update({
        "AS400_RAW_PREFIX": RAW_ROOT, "QUADRINGENT_DESTINATION_DATABASE": "QUADRINGENT",
        "QUADRINGENT_DESTINATION_SCHEMA": "CURATED", "QUADRINGENT_MIRROR_SCHEMA": "CURATED",
        "QUADRINGENT_LOADER_TABLE_SET_JSON": json.dumps([table.to_env_entry()], separators=(",", ":")),
        "SNOWFLAKE_ACCOUNT": "EXAMPLEORG-EXAMPLEACCOUNT", "SNOWFLAKE_USER": "SVC_LOCAL",
        "SNOWFLAKE_ROLE": "QDT_ROLE_LOCAL", "SNOWFLAKE_PRIVATE_KEY_PEM": "offline-only-key",
    })
    return values


def test_loader_reaches_the_snowflake_connection_on_local_storage(admitted, monkeypatch):
    data, env, admission = admitted
    for key, value in loader_environment(env, admission).items():
        monkeypatch.setenv(key, value)
    connect = Mock(side_effect=RuntimeError("stop at the connection"))
    monkeypatch.setattr(loader, "_connect_snowflake", connect)
    with pytest.raises(RuntimeError, match="stop at the connection"):
        loader.main(["--once"])
    kwargs = connect.call_args.kwargs
    assert (kwargs["account"], kwargs["user"], kwargs["role"], kwargs["warehouse"]) == (
        "EXAMPLEORG-EXAMPLEACCOUNT", "SVC_LOCAL", "QDT_ROLE_LOCAL", "QDT_WH_LOCAL")
    assert kwargs["private_key_pem"] == "offline-only-key"
    scopes = list((data / "objects" / LABEL / RAW_ROOT / "loader-scopes").glob("*.json"))
    assert len(scopes) == 1, "the loader scope marker is written on the local store before Snowflake"


def test_loader_refuses_a_binding_authority_of_another_backend(admitted, monkeypatch):
    _data, env, admission = admitted
    values = loader_environment(env, admission)
    entries = json.loads(values["QUADRINGENT_LOADER_TABLE_SET_JSON"])
    entries[0]["capture_binding"]["registry_authority"] = f"s3://{LABEL}/receiver-authority-v1"
    for key, value in {**values, "QUADRINGENT_LOADER_TABLE_SET_JSON": json.dumps(entries)}.items():
        monkeypatch.setenv(key, value)
    table = loader.parse_table_set(json.dumps(entries), require_bound=True)[0]
    with pytest.raises(loader.DestinationLoaderError, match="registry authority"):
        loader._bound_registry(table, StorageBackend.from_environment(env))
