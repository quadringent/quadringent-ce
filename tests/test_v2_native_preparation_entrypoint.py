"""Installed preparation uses server storage and a baked source identity."""

from unittest.mock import Mock
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from quadringent_control_plane.v2 import entrypoint
from quadringent_control_plane.v2.build_identity import BuildIdentity
from test_v2_native_preparation import ROOT, case  # noqa: F401
from test_v2_native_verifications import authority  # noqa: F401


IDENTITY = BuildIdentity("abcde" * 8, "0.4.5")


def config(kind="aws"):
    return {"QUADRINGENT_STORAGE_BACKEND": kind, "QUADRINGENT_RAW_BUCKET": "test-raw",
            "QUADRINGENT_RAW_PREFIX_ROOT": "raw/test", "QUADRINGENT_CHECKPOINT_TABLE": "test-state",
            "QUADRINGENT_CHECKPOINT_BUCKET": "test-state", "AS400_LOCAL_DATA_DIR": "/test/data"}


def test_missing_identity_cannot_invent_installed_preparation(monkeypatch):
    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: None)
    assert entrypoint.build_native_preparer_factory(config(), org_id="org") is None


@pytest.mark.parametrize("kind,location", [("aws", "test-state"), ("gcs", "test-state"), ("local", "/test/data")])
def test_factory_uses_only_configured_storage_and_exact_organization(monkeypatch, kind, location):
    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: IDENTITY)
    constructor = Mock()
    monkeypatch.setattr(entrypoint, "StoredNativePreparer", constructor)
    factory = entrypoint.build_native_preparer_factory(config(kind), org_id="org")
    constructor.assert_not_called()
    assert factory("other-org") is None
    constructor.assert_not_called()
    result = factory("org")
    assert result is constructor.return_value
    args = constructor.call_args.kwargs
    assert (args["storage"].kind, args["storage"].raw_bucket, args["storage"].state_location) == (
        kind, "test-raw", location)
    assert args["raw_prefix_root"] == "raw/test"
    assert args["org_id"] == "org"
    assert (args["producer_revision"], args["producer_version"]) == (IDENTITY.source_revision, IDENTITY.version)
    assert args["storage"].gcs_client is None and args["storage"].aws_client_factory is None


@pytest.mark.parametrize("missing", ["QUADRINGENT_RAW_BUCKET", "QUADRINGENT_RAW_PREFIX_ROOT",
                                     "QUADRINGENT_CHECKPOINT_TABLE"])
def test_missing_required_native_storage_stays_unavailable(monkeypatch, missing):
    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: IDENTITY)
    values = config()
    del values[missing]
    assert entrypoint.build_native_preparer_factory(values, org_id="org") is None


def test_unknown_backend_is_rejected_before_creating_provider_clients(monkeypatch):
    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: IDENTITY)
    with pytest.raises(entrypoint.ConfigurationError):
        entrypoint.build_native_preparer_factory(config("unknown"), org_id="org")


def test_invalid_baked_metadata_fails_with_a_bounded_configuration_error(monkeypatch):
    def invalid():
        raise ValueError("private-path-must-not-be-exposed")

    monkeypatch.setattr(entrypoint, "read_build_identity", invalid)
    with pytest.raises(entrypoint.ConfigurationError) as result:
        entrypoint.build_native_preparer_factory(config(), org_id="org")
    assert "private-path" not in str(result.value)


def test_installed_application_wires_preparation_without_inventing_admission(tmp_path, monkeypatch):
    from quadringent_control_plane.v2.crypto import SecretBox

    factory = Mock()
    builder = Mock(return_value=factory)
    monkeypatch.setattr(entrypoint, "build_native_preparer_factory", builder)
    monkeypatch.setattr(entrypoint, "build_diagnostic_adapters", lambda *args: (None, None))
    monkeypatch.setattr(entrypoint, "build_pipeline_executor", lambda *args: (None, None))
    monkeypatch.setattr(entrypoint, "build_loader_telemetry", lambda *args: None)
    values = {
        entrypoint.ENV_DATABASE_URL: f"sqlite:///{tmp_path / 'native-install.sqlite3'}",
        "QUADRINGENT_V2_ORG_ID": "org",
        "QUADRINGENT_V2_SECRET_KEY": SecretBox.generate_key().decode(),
        "QUADRINGENT_V2_TOKEN_PEPPER": "test-native-preparation-pepper",
    }
    app, engine = entrypoint.build_app(values)
    try:
        builder.assert_called_once_with(values, org_id="org")
        assert app.state.native_verification_preparer_factory is factory
        assert app.state.native_verification_controller_factory is None
        factory.assert_not_called()
    finally:
        engine.dispose()


def test_configured_preparer_persists_real_sql_attempt_and_stays_incomplete_without_admission(case, authority, monkeypatch):
    from sqlalchemy import select
    from quadringent_control_plane.v2 import schema
    from quadringent_control_plane.v2.services.native_verifications import NativeVerificationsService

    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: IDENTITY)
    values = {"QUADRINGENT_STORAGE_BACKEND": "local", "QUADRINGENT_RAW_BUCKET": case.storage.raw_bucket,
              "QUADRINGENT_RAW_PREFIX_ROOT": ROOT, "AS400_LOCAL_DATA_DIR": str(case.data)}
    factory = entrypoint.build_native_preparer_factory(values, org_id="org-one")
    service = NativeVerificationsService(authority[0], org_id="org-one", preparer=factory("org-one"))
    attempt = service.request("pipeline-one", requested_by="fixture", idempotency_key="real-preparation")
    lease = service.claim(attempt["id"], holder="fixture")
    lease = service.prepare(lease)
    assert lease is not None
    with authority[0].connect() as connection:
        row = connection.execute(select(schema.native_verifications).where(
            schema.native_verifications.c.id == attempt["id"])).mappings().one()
    scope = json.loads(row["scope_json"])
    assert scope["copy_boundary"] == case.binding.bootstrap.to_dict()
    assert scope["checkpoint"] == case.checkpoint.to_dict()
    assert scope["producer_revision"] == IDENTITY.source_revision
    refs = json.loads(row["preparation_json"])
    assert {ref["name"] for ref in refs} == {
        "copy_intent", "copy_evidence", "registry", "binding", "checkpoint", "preparation_manifest"}
    for ref in refs:
        raw = case.store.get_bounded(urlsplit(ref["uri"]).path.lstrip("/"), 32 * 1024**2)
        assert len(raw) == ref["byte_count"] and hashlib.sha256(raw).hexdigest() == ref["sha256"]
    result = service.finalize(lease)
    assert result["outcome"] == "incomplete"
    assert result["reason_code"] == "admission_unavailable"
