"""Installed observation is configured without opening provider connections."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2 import entrypoint
from quadringent_control_plane.v2.build_identity import BuildIdentity


@pytest.fixture
def configured(monkeypatch):
    identity = BuildIdentity("abcde" * 8, "0.4.5")
    runtime = {"java": "/server/java", "classpath": "/server/reader.jar", "files": {"/server/reader.jar": "a" * 64}}
    monkeypatch.setattr(entrypoint, "read_build_identity", lambda: identity)
    monkeypatch.setattr(entrypoint, "installed_runtime", lambda values: runtime)
    prepared = SimpleNamespace(storage=StorageBackend("local", "/server/raw", "/server/state"), root="raw/test")
    preparer = Mock(return_value=prepared)
    return identity, runtime, prepared, preparer


def factory(configured, **kw):
    values = kw.pop("values", {})
    return entrypoint.build_native_observer_factory(
        values, org_id="org", engine="engine", secret_box="box", preparer_factory=configured[3], **kw
    )


def test_only_configured_org_gets_a_real_observer_without_provider_io(configured):
    result = factory(configured)
    configured[3].assert_not_called()
    assert result("other-org") is None
    configured[3].assert_not_called()
    observer = result("org")
    assert (observer.org_id, observer.root, observer.storage) == ("org", "raw/test", configured[2].storage)
    assert observer.runtime == configured[1] and observer.seconds == 3600
    configured[3].assert_called_once_with("org")


@pytest.mark.parametrize("missing", ["identity", "runtime", "preparer"])
def test_missing_installed_dependency_keeps_observation_unavailable(configured, monkeypatch, missing):
    if missing == "identity":
        monkeypatch.setattr(entrypoint, "read_build_identity", lambda: None)
    if missing == "runtime":
        monkeypatch.setattr(entrypoint, "installed_runtime", lambda _: None)
    if missing == "preparer":
        configured = (*configured[:3], None)
    assert factory(configured) is None


@pytest.mark.parametrize("value", ["0", "59", "7101", "999999999", "-1", "1.5", "nan", " 60", "true"])
def test_invalid_installed_deadlines_fail_with_bounded_config_error(configured, value):
    with pytest.raises(entrypoint.ConfigurationError):
        factory(configured, values={"QUADRINGENT_V2_NATIVE_TIMEOUT_SECONDS": value})


def test_installed_application_wires_observation_separately_from_admission(tmp_path, monkeypatch):
    from quadringent_control_plane.v2.crypto import SecretBox

    prepared = Mock()
    observed = Mock()
    builder = Mock(return_value=observed)
    monkeypatch.setattr(entrypoint, "build_native_preparer_factory", lambda *a, **k: prepared)
    monkeypatch.setattr(entrypoint, "build_native_observer_factory", builder)
    monkeypatch.setattr(entrypoint, "build_diagnostic_adapters", lambda *a: (None, None))
    monkeypatch.setattr(entrypoint, "build_pipeline_executor", lambda *a: (None, None))
    monkeypatch.setattr(entrypoint, "build_loader_telemetry", lambda *a: None)
    values = {
        entrypoint.ENV_DATABASE_URL: f"sqlite:///{tmp_path / 'observer.sqlite3'}",
        "QUADRINGENT_V2_ORG_ID": "org",
        "QUADRINGENT_V2_SECRET_KEY": SecretBox.generate_key().decode(),
        "QUADRINGENT_V2_TOKEN_PEPPER": "synthetic-native-observer-pepper",
    }
    app, engine = entrypoint.build_app(values)
    try:
        assert app.state.native_verification_observer_factory is observed
        assert app.state.native_verification_controller_factory is None
        from sqlalchemy import select
        with engine.connect() as connection:
            assert connection.execute(select(entrypoint.schema.organizations.c.native_context_enforced)).scalar_one() is True
        assert builder.call_args.args == (values,)
        assert builder.call_args.kwargs["engine"] is engine
        assert builder.call_args.kwargs["preparer_factory"] is prepared
        prepared.assert_not_called()
        observed.assert_not_called()
    finally:
        engine.dispose()


@pytest.mark.parametrize("value,expected", [(None, None), ("true", True), ("false", False)])
def test_context_tracking_startup_choice_is_explicit(value, expected):
    values = {} if value is None else {"QUADRINGENT_V2_NATIVE_CONTEXT_TRACKING": value}
    assert entrypoint._native_tracking_preference(values) is expected


@pytest.mark.parametrize("value", ["", "True", "FALSE", "0", "1", " true ", True, 1])
def test_invalid_tracking_preference_is_rejected(value):
    with pytest.raises(entrypoint.ConfigurationError):
        entrypoint._native_tracking_preference({"QUADRINGENT_V2_NATIVE_CONTEXT_TRACKING": value})


def test_fresh_installation_can_explicitly_keep_tracking_disabled(tmp_path):
    from quadringent_control_plane.v2 import db, schema
    from sqlalchemy import select

    dsn = f"sqlite:///{tmp_path / 'opt-out.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    try:
        entrypoint.ensure_organization(engine, "org", native_context_tracking=False)
        with engine.connect() as connection:
            assert connection.execute(select(schema.organizations.c.native_context_enforced)).scalar_one() is False
    finally:
        engine.dispose()
