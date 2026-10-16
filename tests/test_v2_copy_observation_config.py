"""Installed operator configuration reaches the real copy executor."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from quadringent_control_plane.v2 import entrypoint

ENV = "QUADRINGENT_V2_COPY_OBSERVATIONS_JSON"
REQUEST = {
    "format_version": "quadringent-copy-observation-request-v1",
    "mode": "fixed-single-member-format-v1",
    "source": {"library": "LIB", "file": "ORDERS", "member": "ORDERS", "format": "ORDERS"},
    "witness_window": {"receiver_library": "LIB", "receiver_name": "R1", "start": "1", "end": "20"},
    "max_metadata_bytes": 1048576, "max_seconds": 60,
}


def settings(request=None):
    return {"format_version": "quadringent-copy-observation-settings-v1",
            "pipelines": {"pipeline-one": deepcopy(REQUEST if request is None else request)}}


def context(**changes):
    value = dict(pipeline_id="pipeline-one", schema_name="LIB", table_name="ORDERS",
                 table_id="table-one", source_id="source-one", journal_library="LIB", journal_name="JRN")
    value.update(changes)
    return SimpleNamespace(**value)


def factory(value):
    return entrypoint._copy_observation_request_factory({ENV: json.dumps(value)})


def test_observation_mode_is_opt_in_and_unknown_pipeline_has_no_request():
    assert entrypoint._copy_observation_request_factory({}) is None
    configured = factory(settings())
    assert configured(context(pipeline_id="pipeline-other")) is None
    assert configured(context()) == REQUEST


def test_factory_never_shares_mutable_request_between_executions():
    value = settings()
    configured = factory(value)
    value["pipelines"]["pipeline-one"]["source"]["member"] = "OTHER"
    first = configured(context())
    first["source"]["member"] = "CHANGED"
    assert configured(context()) == REQUEST


@pytest.mark.parametrize("changes", [{"schema_name": "OTHER"}, {"table_name": "OTHER"}])
def test_configured_request_cannot_move_to_another_physical_table(changes):
    with pytest.raises(entrypoint.ConfigurationError, match="copy observation"):
        factory(settings())(context(**changes))


@pytest.mark.parametrize("raw", [
    "{}", "[]", "null", "{bad",
    '{"format_version":"quadringent-copy-observation-settings-v1","pipelines":{},"pipelines":{}}',
    '{"format_version":"quadringent-copy-observation-settings-v1","pipelines":{"x":NaN}}',
    '{"format_version":"quadringent-copy-observation-settings-v1","pipelines":{},"admitted":true}',
    "x" * 65537,
], ids=["empty", "array", "null", "syntax", "duplicate", "nan", "unknown", "oversized"])
def test_bad_or_unknown_operator_configuration_is_not_silently_ignored(raw):
    with pytest.raises(entrypoint.ConfigurationError) as failure:
        entrypoint._copy_observation_request_factory({ENV: raw})
    assert "x" * 100 not in str(failure.value)


def test_invalid_request_fails_at_configuration_before_any_provider_access():
    value = settings()
    value["pipelines"]["pipeline-one"]["native_admission"] = True
    with pytest.raises(entrypoint.ConfigurationError):
        factory(value)


def test_build_pipeline_executor_parses_config_before_provider_clients(monkeypatch):
    monkeypatch.setattr(entrypoint, "in_cluster", lambda: True)
    monkeypatch.setattr(entrypoint, "_executor_clients_from_environment",
                        lambda: pytest.fail("must fail before any client"))
    with pytest.raises(entrypoint.ConfigurationError):
        entrypoint.build_pipeline_executor({ENV: "{bad"}, None, None)


def test_installed_app_passes_operator_requests_to_the_real_executor(tmp_path, monkeypatch):
    from test_v2_entrypoint import _environ, _fake_service_account

    _fake_service_account(tmp_path, monkeypatch)
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    values = _environ(
        tmp_path, QUADRINGENT_SITE_ID="fixture-site",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/capture@sha256:" + "0" * 64,
        QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT="fixture-capture",
        QUADRINGENT_RAW_BUCKET="fixture-raw", QUADRINGENT_RAW_PREFIX_ROOT="fixture/streams",
        QUADRINGENT_STORAGE_BACKEND="aws", QUADRINGENT_CHECKPOINT_TABLE="fixture-checkpoints",
        **{ENV: json.dumps(settings())},
    )
    app, engine = entrypoint.build_app(values)
    try:
        executor = app.state.pipeline_executor
        assert executor is not None
        configured = executor._config.copy_observation_request_factory
        assert configured(context()) == REQUEST
        assert configured(context(pipeline_id="unconfigured")) is None
    finally:
        engine.dispose()
