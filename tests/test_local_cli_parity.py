"""The host mirrors of managed-product logic stay identical to it (setup script, columns, table set, reader env)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

import pytest

from local_cli_support import DockerStub
from quadringent.capture_binding import CaptureBinding
from quadringent.local import discovery as disc
from quadringent.local import snowflake_setup as setup
from quadringent.local import tableset
from quadringent.local.discovery import declared_columns
from quadringent.reader_scope import reader_checkpoint_stream_key
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.table_discovery import DiscoveredColumn

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from quadringent_control_plane.v2.executor.boundary import JournalBoundary  # noqa: E402
from quadringent_control_plane.v2.executor.evidence import evidence_key as managed_evidence_key  # noqa: E402
from quadringent_control_plane.v2.executor.manifests import (  # noqa: E402
    LoaderTableSpec, ReaderDesiredSpec, TableBootstrap, build_reader_deployment)
from quadringent_control_plane.v2.services.destination_verifier import (  # noqa: E402
    validated_destination_scope, warehouse_name_for)
from quadringent_control_plane.v2.services.destinations import _build_setup_script, _generate_rsa_key_pair  # noqa: E402
from quadringent_control_plane.v2.services.tables import _auto_discovered_columns  # noqa: E402


def drop_header_line(script: str) -> str:
    lines = script.splitlines()
    assert lines[1].startswith("-- Generated on")
    return "\n".join(lines[:1] + lines[2:])


@pytest.mark.parametrize("database, schema", [("QUADRINGENT", None), ("quadringent", "mirror1"), ("DB_1", "S")])
def test_the_setup_script_is_the_managed_copy_merge_script(database, schema):
    _, public = _generate_rsa_key_pair()
    kwargs = dict(snowflake_account="exampleorg-exampleaccount", role_name="QDT_ROLE_ABCD1234",
                  user_name="QDT_SVC_ABCD1234", public_key_pem=public, destination_database=database,
                  destination_schema=schema)
    managed = _build_setup_script(destination_mode="copy_merge", mirror_option="loader_merge", **kwargs)
    ours = setup.build_setup_script(**kwargs)
    assert drop_header_line(ours) == drop_header_line(managed)
    assert "Generated on this machine" in ours
    assert setup.public_key_body(public) in ours and "PRIVATE" not in ours


def test_scope_validation_and_warehouse_naming_match_the_managed_product():
    for database, schema in (("quadringent", None), ("db", "s1")):
        assert setup.validated_scope(database, schema) == validated_destination_scope(database, schema)
    for bad in ("1abc", "a-b", "", "x" * 64):
        with pytest.raises(Exception):
            setup.validated_scope(bad, None)
        with pytest.raises(Exception):
            validated_destination_scope(bad, None)
    assert "QDT_WH_" + "ABCD1234" == warehouse_name_for("QDT_ROLE_ABCD1234")


def test_the_generated_key_is_pkcs8_rsa_2048_and_the_public_half_matches(tmp_path):
    from cryptography.hazmat.primitives import serialization

    path = tmp_path / "key.p8"
    public = setup.create_key_pair(path, DockerStub())
    private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert private.key_size == 2048 and path.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert serialization.load_pem_public_key(public.encode()).public_numbers() == private.public_key().public_numbers()
    with pytest.raises(setup.KeyGenerationError):
        setup.create_key_pair(path, DockerStub())  # never overwrites


def test_without_cryptography_openssl_writes_the_key_itself_and_only_paths_are_arguments(tmp_path, monkeypatch):
    for name in [m for m in sys.modules if m == "cryptography" or m.startswith("cryptography.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "cryptography", None)  # import now fails
    runner = DockerStub()
    path = tmp_path / "key.p8"
    public = setup.create_key_pair(path, runner)
    assert "BEGIN PUBLIC KEY" in public
    generate, derive = [argv for argv, _, _ in runner.invocations]
    assert generate[:4] == ("openssl", "genpkey", "-algorithm", "RSA") and generate[-2:] == ("-out", str(path))
    assert derive == ("openssl", "pkey", "-in", str(path), "-pubout")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    runner.available_tools = frozenset()
    with pytest.raises(setup.KeyGenerationError, match="openssl"):
        setup.create_key_pair(tmp_path / "other.p8", runner)


COLUMN_CASES = {
    "supported": (("ID", "DECIMAL", 9, 0, False), ("NAME", "VARCHAR", 40, None, True), ("AT", "TIMESTAMP", None, None, True),
                  ("N", "INTEGER", None, None, False), ("D", "DATE", None, None, True), ("C", "CHAR", 3, None, True)),
    "unsupported type": (("ID", "DECIMAL", 9, 0, False), ("X", "XMLTYPE", None, None, True)),
    "duplicate name": (("ID", "INTEGER", None, None, False), ("ID", "INTEGER", None, None, False)),
    "empty": (),
    "numeric": (("N", "NUMERIC", 12, 2, False),),
}


@pytest.mark.parametrize("case", sorted(COLUMN_CASES))
def test_declared_columns_equal_the_managed_auto_declaration(case):
    columns = tuple(DiscoveredColumn(*entry) for entry in COLUMN_CASES[case])
    assert declared_columns(columns) == _auto_discovered_columns(columns)


def test_columns_parsed_from_the_worker_protocol_declare_like_the_managed_path():
    from local_cli_support import discover_output, table_row

    table = disc.parse_discovery(discover_output(table_row()))[0]
    assert declared_columns(table.columns) == _auto_discovered_columns(table.columns)


def test_evidence_key_matches_the_managed_formula():
    assert tableset.evidence_key("raw/", "tbl1", "run1") == managed_evidence_key("raw/", "tbl1", "run1")


IDENTITY = ReceiverIdentity("QDCLIB", "QSQJRN", "QDCLIB", "QSQJRN0001", "native-test-v1", "generation-a")
OBSERVED = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)


def managed_admission(backend="gcs", bucket="quadringent-raw-example", raw_prefix="raw"):
    """An admission as the container would print it, bound to a managed backend authority for the reader comparison."""
    scope = reader_checkpoint_stream_key(storage_backend=backend, source_id="src1", journal_library="QDCLIB",
                                         journal_name="QSQJRN", raw_bucket=bucket, raw_prefix=raw_prefix)
    point = BoundCheckpoint(scope, "epoch-1", IDENTITY, 4200)
    scheme = "gcs" if backend == "gcs" else "local"
    binding = CaptureBinding(scope, "epoch-1", "ibmi", "QSQJRN", f"{scheme}://{bucket}/receiver-authority-v1", point)
    boundary = JournalBoundary("QDCLIB", "QSQJRN0001", 4200, OBSERVED, receiver_identity=IDENTITY).with_binding(binding)
    run_id = "0d6a9c2e-1a2b-4c3d-9e0f-1234567890ab"
    return {"binding": binding.to_dict(), "boundary": boundary.to_dict(), "run_id": run_id,
            "evidence_key": managed_evidence_key(raw_prefix, "tbl1", run_id), "raw_prefix_root": raw_prefix,
            "stream_key": scope, "table_id": "tbl1", "pipeline_id": "pip1"}, boundary


def test_the_loader_table_entry_equals_loader_table_spec_to_env_entry():
    admission, boundary = managed_admission()
    columns = tuple(declared_columns(tuple(DiscoveredColumn(*entry) for entry in COLUMN_CASES["supported"])))
    managed = LoaderTableSpec(
        table_id="tbl1", schema_name="QDCLIB", table_name="ORDERS", key_columns=("ID",), columns=columns,
        evidence_key=admission["evidence_key"], pipeline_id="pip1", run_id=admission["run_id"],
        capture_binding=boundary.capture_binding,
    ).to_env_entry()
    ours = tableset.loader_table_entry(admission, schema="QDCLIB", table="ORDERS", key_columns=("ID",), columns=columns)
    assert json.loads(tableset.dumps([ours])) == json.loads(json.dumps([managed], sort_keys=True))


def test_the_capture_identity_env_equals_the_managed_reader_deployment_env():
    admission, boundary = managed_admission()
    managed_spec = ReaderDesiredSpec(
        source_id="src1", journal_library="QDCLIB", journal_name="QSQJRN", image="registry.example.test/c:1",
        namespace="quadringent", storage_backend="gcs", source_time_zone="Europe/Paris", raw_prefix="raw",
        reader_timeout_seconds=300,
        tables=(TableBootstrap(table_id="tbl1", schema_name="QDCLIB", table_name="ORDERS", boundary=boundary),),
        destination_secret_ref="d", ibmi_secret_ref="i", service_account_name="sa", ibmi_host="ibmi.example.test",
        ibmi_user="SVCUSER", raw_bucket="quadringent-raw-example", checkpoint_location="cp")
    container = build_reader_deployment(managed_spec)["spec"]["template"]["spec"]["containers"][0]
    managed = {entry["name"]: entry["value"] for entry in container["env"]}
    ours = tableset.capture_identity_env(admission, table="ORDERS", journal_library="QDCLIB", journal_name="QSQJRN")
    for key, value in ours.items():
        assert managed[key] == value, key
    assert set(ours) == {"AS400_STREAM_KEY", "AS400_CHECKPOINT_SCOPE", "AS400_JOURNAL_LIBRARY", "AS400_JOURNAL_NAME",
                         "AS400_RAW_PREFIX", "AS400_RECEIPTED_SCANS", "AS400_BOOTSTRAP_RECEIVER",
                         "AS400_BOOTSTRAP_RECEIVER_LIBRARY", "AS400_BOOTSTRAP_SEQUENCE",
                         "AS400_CAPTURE_BINDING_JSON", "AS400_BOUNDARY_JSON"}


def test_a_malformed_or_foreign_admission_is_refused():
    admission, _ = managed_admission()
    assert tableset.parse_admission(admission, table_id="tbl1", pipeline_id="pip1") == admission
    for change in ({"run_id": "not-a-uuid"}, {"table_id": "other"}, {"evidence_key": "raw/x.json"}, {"boundary": {}}):
        with pytest.raises(tableset.AdmissionError):
            tableset.parse_admission({**admission, **change}, table_id="tbl1", pipeline_id="pip1")
    with pytest.raises(tableset.AdmissionError):
        tableset.parse_admission({"run_id": admission["run_id"]}, table_id="tbl1", pipeline_id="pip1")


def test_the_public_key_is_one_complete_base64_line():
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from quadringent.local.snowflake_setup import public_key_body as _public_key_body
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    pem = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = _public_key_body(pem)
    assert "\n" not in body and body == base64.b64encode(der).decode("ascii")
