"""Le harnais lance les programmes du produit avec un secret sur stdin."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from quadringent_qualification.adapters import CaptureBoundary
from quadringent_qualification.config import (
    CaptureConfig, RunConfig, SourceConfig, StorageConfig, WarehouseConfig,
)
from quadringent_qualification.real_capture import DockerCaptureRunner
from quadringent_qualification.schema import Column, TableSchema


RUN_ID = "123e4567-e89b-42d3-a456-4266b4174000"
IMAGE = "registry.example/quadringent/capture@sha256:" + "a" * 64
PASSWORD = "fixture-only-password"


def _config(tmp_path: Path, *, backend: str = "gcs") -> RunConfig:
    secret = tmp_path / "source.json"
    secret.write_text(json.dumps({"host": "ibmi.example", "user": "QUALUSER", "password": PASSWORD}))
    secret.chmod(0o600)
    return RunConfig(
        run_id=RUN_ID,
        table=TableSchema("QUALTEST.QUALIF_ORDERS", (Column("ORDER_ID", "integer"),), "ORDER_ID"),
        source=SourceConfig(
            driver="ibmi_java", library_whitelist=("QUALTEST",), connection_secret_file=str(secret),
            journal_library="QUALTEST", journal_name="QUALJRN", source_time_zone="UTC",
        ),
        capture=CaptureConfig(image=IMAGE, max_seconds=90),
        storage=StorageConfig(
            backend=backend, bucket="qual-bucket", raw_prefix=f"qualification/{RUN_ID}",
            checkpoint_location="qual-state",
        ),
        warehouse=WarehouseConfig("snowflake", "/private/snowflake.json", "QUALDB", "QUALSCHEMA"),
        steps=("snapshot", "capture"),
    )


def _boundary() -> CaptureBoundary:
    return CaptureBoundary(
        receiver_library="RECVLIB", receiver_name="RECV0001", next_sequence=101,
        observed_at=datetime(2026, 9, 29, 12, 34, tzinfo=timezone.utc),
    )


class RecordingDocker:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return CompletedProcess(command, self.returncode, self.stdout, "")


def _container_env(command: list[str], options: dict[str, object]) -> dict[str, str]:
    process_env = options["env"]
    assert isinstance(process_env, dict)
    names = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--env"]
    assert all(isinstance(name, str) and "=" not in name for name in names)
    return {name: process_env[name] for name in names}


def test_snapshot_uses_original_boundary_and_password_only_on_stdin(tmp_path: Path) -> None:
    docker = RecordingDocker(json.dumps({
        "event": "initial_copy_completed", "run_id": RUN_ID,
        "table_id": f"qual-{RUN_ID}", "rows_copied": 7,
    }) + "\n")
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)

    result = runner.run(label="snapshot", max_seconds=90, bootstrap=_boundary(), env={})

    assert result.exit_code == 0
    assert result.event_count == 7
    assert result.events == ()
    command, options = docker.calls[0]
    container_env = _container_env(command, options)
    assert IMAGE in command
    assert "/app/quadringent_initial_copy_job.py" in command
    assert "--network" in command and "host" in command
    assert container_env["AS400_BOOTSTRAP_RECEIVER_LIBRARY"] == "RECVLIB"
    assert container_env["AS400_BOOTSTRAP_RECEIVER"] == "RECV0001"
    assert container_env["AS400_BOOTSTRAP_SEQUENCE"] == "100"
    assert container_env["AS400_BOOTSTRAP_OBSERVED_AT"] == "2026-09-29T12:34:00+00:00"
    assert container_env["AS400_RAW_PREFIX"] == f"qualification/{RUN_ID}"
    assert container_env["AS400_EVIDENCE_KEY"] == f"qualification/{RUN_ID}/qual-{RUN_ID}/evidence/{RUN_ID}.json"
    assert container_env["AS400_SOURCE_TIME_ZONE"] == "UTC"
    assert options["input"] == PASSWORD + "\n"
    assert PASSWORD not in " ".join(command)
    assert PASSWORD not in str(options["env"])
    assert PASSWORD not in result.log


def test_capture_restarts_at_same_boundary_with_run_scoped_checkpoint(tmp_path: Path) -> None:
    docker = RecordingDocker('{"event":"capture_finished","metrics":{"events_published":3}}\n')
    runner = DockerCaptureRunner(_config(tmp_path, backend="s3"), runner=docker)

    first = runner.run(label="capture-1", max_seconds=90, bootstrap=_boundary(), env={})
    second = runner.run(label="capture-2", max_seconds=90, bootstrap=_boundary(), env={})

    assert first.event_count == second.event_count == 3
    first_command, first_options = docker.calls[0]
    second_command, second_options = docker.calls[1]
    first_env = _container_env(first_command, first_options)
    second_env = _container_env(second_command, second_options)
    assert first_env["AS400_BOOTSTRAP_SEQUENCE"] == second_env["AS400_BOOTSTRAP_SEQUENCE"] == "101"
    assert first_env["AS400_STREAM_KEY"] == second_env["AS400_STREAM_KEY"]
    assert RUN_ID in first_env["AS400_STREAM_KEY"]
    assert first_env["AS400_RAW_PREFIX"] == f"qualification/{RUN_ID}/qualif_orders/journal"
    assert first_env["AS400_CHECKPOINT_TABLE"] == "qual-state"
    assert first_env["AS400_RECEIPTED_SCANS"] == "true"
    assert "/app/as400_continuous_capture.py" in first_command
    assert first_command[first_command.index("--max-seconds") + 1] == "90"
    assert second_options["input"] == PASSWORD + "\n"


def test_another_run_cannot_reuse_the_same_checkpoint_key(tmp_path: Path) -> None:
    docker = RecordingDocker('{"event":"capture_finished","metrics":{"events_published":0}}\n')
    original = _config(tmp_path)
    other_run_id = "123e4567-e89b-42d3-a456-4266b4174001"
    other = replace(
        original, run_id=other_run_id,
        storage=replace(original.storage, raw_prefix=f"qualification/{other_run_id}"),
    )
    DockerCaptureRunner(original, runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    DockerCaptureRunner(other, runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    first = _container_env(*docker.calls[0])
    second = _container_env(*docker.calls[1])
    assert first["AS400_STREAM_KEY"] != second["AS400_STREAM_KEY"]
    assert first["AS400_RAW_PREFIX"] != second["AS400_RAW_PREFIX"]


@pytest.mark.parametrize("label,stdout", [
    ("snapshot", "{}\n"),
    ("capture-1", '{"event":"capture_poll","event_count":3}\n'),
])
def test_successful_docker_exit_without_product_receipt_fails_closed(
    tmp_path: Path, label: str, stdout: str,
) -> None:
    docker = RecordingDocker(stdout)
    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run(
        label=label, max_seconds=90, bootstrap=_boundary(), env={},
    )
    assert result.exit_code != 0
    assert result.event_count == 0


def test_invalid_run_scope_or_timezone_is_refused_before_docker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = RecordingDocker("")
    bad_scope = replace(config, storage=replace(config.storage, raw_prefix="qualification/another-run"))
    with pytest.raises(ValueError, match="run prefix"):
        DockerCaptureRunner(bad_scope, runner=docker)
    bad_time_zone = replace(config, source=replace(config.source, source_time_zone=None))
    with pytest.raises(ValueError, match="time zone"):
        DockerCaptureRunner(bad_time_zone, runner=docker)
    assert docker.calls == []


def test_extra_environment_cannot_override_the_run_scope(tmp_path: Path) -> None:
    docker = RecordingDocker("")
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)
    with pytest.raises(ValueError, match="invocation"):
        runner.run(label="capture-1", max_seconds=90, bootstrap=_boundary(),
                   env={"AS400_RAW_BUCKET": "another-bucket"})
    assert docker.calls == []


def test_timed_out_docker_run_force_removes_its_named_container(tmp_path: Path) -> None:
    class TimedOutDocker:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, command: list[str], **_kwargs: object) -> CompletedProcess[str]:
            self.calls.append(command)
            if command[1] == "run":
                raise TimeoutExpired(command, 90)
            if command[1] == "container":
                return CompletedProcess(command, 1, "[]", f"error: no such object: {command[-1]}")
            return CompletedProcess(command, 0, "", "")

    docker = TimedOutDocker()
    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )

    assert result.exit_code == 124
    name = docker.calls[0][docker.calls[0].index("--name") + 1]
    assert docker.calls[1] == ["docker", "rm", "--force", "--volumes", name]


def test_freshness_capture_runs_one_poll_without_waiting_for_general_window(tmp_path: Path) -> None:
    from quadringent.continuous import ContinuousCaptureService, PollResult

    docker = RecordingDocker('{"event":"capture_finished","metrics":{"events_published":1}}\n')
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)
    result = runner.run_freshness(
        label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25,
    )
    assert result.exit_code == 0 and result.event_count == 1
    command, options = docker.calls[0]
    assert command[command.index("--max-polls") + 1] == "1"
    assert "--max-seconds" not in command
    assert options["timeout"] == 7.25
    assert _container_env(command, options)["AS400_READER_TIMEOUT_SECONDS"] == "15"

    # La vraie boucle produit termine après cette lecture, même sans événement.
    calls = []
    service = ContinuousCaptureService(None, None, None, None, max_entries=1,
        sleep=lambda seconds: pytest.fail("une seule lecture ne doit pas attendre"))
    service.run_once = lambda: (calls.append("read") or PollResult("idle", None))
    service.run(max_polls=int(command[command.index("--max-polls") + 1]))
    assert calls == ["read"]


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 91])
def test_freshness_capture_refuses_invalid_budget_before_docker(tmp_path: Path, timeout) -> None:
    docker = RecordingDocker("")
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)
    with pytest.raises(ValueError):
        runner.run_freshness(label="capture-1", bootstrap=_boundary(), timeout_seconds=timeout)
    assert docker.calls == []


def test_freshness_docker_success_without_final_receipt_is_not_accepted(tmp_path: Path) -> None:
    docker = RecordingDocker('{"event":"capture_poll","event_count":1}\n')
    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run_freshness(
        label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25,
    )
    assert result.exit_code != 0 and result.event_count == 0


def test_freshness_timeout_cleans_named_container_without_retry(tmp_path: Path) -> None:
    calls = []

    def docker(command, **options):
        calls.append(command)
        if command[1] == "run":
            assert options["timeout"] == 7.25
            raise TimeoutExpired(command, 7.25)
        if command[1] == "container":
            return CompletedProcess(command, 1, "[]", f"error: no such object: {command[-1]}")
        return CompletedProcess(command, 0, "", "")

    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run_freshness(
        label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25,
    )
    assert result.exit_code == 124
    assert [command[1] for command in calls] == ["run", "rm", "container"]
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "--force", "--volumes", name]


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "interrupt"])
@pytest.mark.parametrize("absence", ["no such object", "no such container"])
def test_failed_capture_confirms_exact_container_absence_before_return(
    tmp_path: Path, failure: str, absence: str,
) -> None:
    calls = []

    def docker(command, **options):
        calls.append((command, options))
        if command[1] == "run":
            if failure == "timeout":
                raise TimeoutExpired(command, 7.25)
            if failure == "interrupt":
                raise KeyboardInterrupt()
            return CompletedProcess(command, 1, "", "client disconnected")
        if command[1] == "rm":
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 1, "[]", f"error: {absence}: {command[-1]}")

    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            runner.run_freshness(label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25)
    else:
        result = runner.run_freshness(label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25)
        assert result.exit_code == (124 if failure == "timeout" else 1)
    commands = [command for command, _ in calls]
    assert [command[1] for command in commands] == ["run", "rm", "container"]
    name = commands[0][commands[0].index("--name") + 1]
    assert commands[1] == ["docker", "rm", "--force", "--volumes", name]
    assert commands[2] == ["docker", "container", "inspect", name]
    assert calls[0][1]["timeout"] == 7.25
    assert PASSWORD not in str(commands)
    assert "input" not in calls[1][1] and "input" not in calls[2][1]


@pytest.mark.parametrize("inspect_result", [
    CompletedProcess([], 0, '[{"State":{"Running":true}}]', ""),
    CompletedProcess([], 1, "", "permission denied"),
])
def test_nonzero_capture_cannot_return_while_absence_is_unverified(
    tmp_path: Path, inspect_result,
) -> None:
    calls = []

    def docker(command, **options):
        calls.append(command)
        if command[1] == "container":
            return inspect_result
        return CompletedProcess(command, 1 if command[1] == "run" else 0, "", "")

    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        DockerCaptureRunner(_config(tmp_path), runner=docker).run_freshness(
            label="capture-1", bootstrap=_boundary(), timeout_seconds=7.25,
        )
    assert [command[1] for command in calls] == ["run", "rm", "container"]


@pytest.mark.parametrize("default_region,fallback,expected", [
    ("eu-west-3", "us-west-2", "eu-west-3"),
    (None, "eu-west-3", "eu-west-3"),
])
def test_s3_capture_inherits_only_selected_region(tmp_path, monkeypatch, default_region, fallback, expected):
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    if default_region is not None:
        monkeypatch.setenv("AWS_DEFAULT_REGION", default_region)
    monkeypatch.setenv("AWS_REGION", fallback)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-key-never-forwarded")
    docker = RecordingDocker('{"event":"capture_completed","events":0}\n')
    DockerCaptureRunner(_config(tmp_path, backend="s3"), runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    command, options = docker.calls[0]
    child = _container_env(command, options)
    assert child["AWS_DEFAULT_REGION"] == expected
    assert "AWS_REGION" not in child and "AWS_ACCESS_KEY_ID" not in options["env"]
    assert expected not in command


def test_gcs_does_not_inherit_aws_region(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-3")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    docker = RecordingDocker('{"event":"capture_completed","events":0}\n')
    DockerCaptureRunner(_config(tmp_path), runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    child = _container_env(*docker.calls[0])
    assert "AWS_DEFAULT_REGION" not in child and "AWS_REGION" not in child


@pytest.mark.parametrize("region", ["eu-west-3\n", "", "https://example.test"])
def test_invalid_s3_region_rejected_before_docker(tmp_path, monkeypatch, region):
    monkeypatch.setenv("AWS_DEFAULT_REGION", region)
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    docker = RecordingDocker("")
    with pytest.raises(ValueError, match="AWS region"):
        DockerCaptureRunner(_config(tmp_path, backend="s3"), runner=docker).run(
            label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
        )
    assert docker.calls == []


def test_missing_s3_region_has_no_invented_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    docker = RecordingDocker('{"event":"capture_completed","events":0}\n')
    DockerCaptureRunner(_config(tmp_path, backend="s3"), runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    assert "AWS_DEFAULT_REGION" not in _container_env(*docker.calls[0])


def test_continuous_warm_readiness_requires_completed_scan_and_cleanup(tmp_path):
    import io
    from types import SimpleNamespace
    commands = []
    process = SimpleNamespace(
        stdin=io.StringIO(), stdout=io.StringIO(
            '{"event":"capture_started"}\n'
            '{"event":"capture_poll","status":"empty_scan","event_count":0,"metrics":{"polls":1}}\n'),
        poll=lambda: None, wait=lambda **kw: 0,
    )
    def factory(command, **kwargs):
        commands.append(command)
        assert kwargs['stderr'] == __import__('subprocess').PIPE
        return process
    cleanup = []
    def runner(command, **kwargs):
        cleanup.append(command)
        return CompletedProcess(command, 1 if 'inspect' in command else 0, '', 'No such container')
    capture = DockerCaptureRunner(_config(tmp_path), runner=runner, process_factory=factory, diagnostic_directory=tmp_path / "diagnostics")
    result = capture.prepare_freshness(bootstrap=_boundary())
    assert result.exit_code == 0 and result.log == 'capture_poll_completed'
    assert '--max-polls' not in commands[0]
    assert '--pull=never' in commands[0]
    capture.close_freshness()
    assert cleanup[0][:4] == ['docker', 'rm', '--force', '--volumes']
    assert cleanup[0][-1] == commands[0][commands[0].index('--name') + 1]
    assert capture._warm is None


def test_started_event_without_scan_never_proves_readiness(tmp_path):
    import io
    from types import SimpleNamespace
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO('{"event":"capture_started"}\n'),
                              poll=lambda: None, wait=lambda **kw: 0)
    capture = DockerCaptureRunner(_config(tmp_path), process_factory=lambda *a, **kw: process, diagnostic_directory=tmp_path / "diagnostics")
    capture._cleanup_container = lambda *a: None
    result = capture.prepare_freshness(bootstrap=_boundary())
    assert result.exit_code == 0 and result.log == "capture_worker_exited"
    capture.close_freshness()


@pytest.mark.parametrize('failure,expected,reason', [
    (TimeoutExpired('docker', 1), 124, 'capture_timeout'),
    (OSError('private external text'), 127, 'capture_os_error'),
])
def test_capture_failure_diagnostics_are_safe(tmp_path, failure, expected, reason):
    def runner(command, **kwargs):
        if 'run' in command:
            raise failure
        return CompletedProcess(command, 1 if 'inspect' in command else 0, '', 'No such container')
    capture = DockerCaptureRunner(_config(tmp_path), runner=runner)
    result = capture.run_freshness(label='capture-1', bootstrap=_boundary(), timeout_seconds=1)
    assert (result.exit_code, result.log) == (expected, reason)
    assert 'private' not in result.log


@pytest.mark.parametrize('returncode,pump_alive,record,expected', [
    (None, True, None, 0), (137, True, None, 137),
    (None, False, None, 137), (None, True, {'status': 'failed'}, 6),
    (None, True, {'status': 'empty_scan'}, 0),
])
def test_live_guard_checks_process_pump_and_retains_poll_evidence(tmp_path, returncode, pump_alive, record, expected):
    from types import SimpleNamespace
    capture = DockerCaptureRunner(_config(tmp_path))
    capture._warm = SimpleNamespace(poll=lambda: returncode, wait=lambda **kw: 137)
    capture._warm_thread = SimpleNamespace(is_alive=lambda: pump_alive)
    if record is not None:
        capture._warm_lines.put(record)
    assert capture.check_freshness().exit_code == expected
    if record is not None:
        assert capture._warm_lines.get_nowait() == record


def test_real_tail_poll_is_warm_readiness_without_new_raw(tmp_path):
    """Un journal rattrapé n'a aucune fenêtre à publier avant la DML."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from quadringent.continuous import ContinuousCaptureService, ReceiverSnapshot
    from quadringent.contract import JournalPosition
    tail = JournalPosition('RCV2', 1323)
    catalog = Mock()
    catalog.snapshot.return_value = [ReceiverSnapshot('QUALTEST', 'RCV2', 1262, 1323)]
    runner = Mock()
    from quadringent.object_store import FileObjectStore
    coordinator = SimpleNamespace(store=FileObjectStore(tmp_path / 'raw'), capture=Mock())
    service = ContinuousCaptureService(catalog, runner, coordinator,
                                       SimpleNamespace(load=lambda: tail), max_entries=100)
    result = service.run_once()
    assert result.status == 'idle'
    catalog.snapshot.assert_called_once_with(required_receiver='RCV2')
    runner.capture.assert_not_called()
    coordinator.capture.assert_not_called()
    capture = DockerCaptureRunner(_config(tmp_path))
    capture._warm = SimpleNamespace(poll=lambda: None)
    capture._warm_lines.put({'event': 'capture_poll', 'status': result.status,
                             'window': None, 'event_count': result.event_count,
                             'metrics': service.metrics.snapshot()})
    ready = capture._wait_warm_poll(1)
    assert (ready.exit_code, ready.log, ready.event_count) == (0, 'capture_poll_completed', 0)


@pytest.mark.parametrize('status,metrics', [
    ('idle', {'polls': 0, 'idle_polls': 1, 'last_lag_sequences': 0}),
    ('idle', {'polls': 1, 'idle_polls': 0, 'last_lag_sequences': 0}),
    ('idle', {'polls': 1, 'idle_polls': 1, 'last_lag_sequences': 1}),
    ('not_a_product_status', {'polls': 1}),
])
def test_unproven_idle_or_unknown_status_never_proves_readiness(tmp_path, status, metrics):
    from types import SimpleNamespace
    capture = DockerCaptureRunner(_config(tmp_path))
    capture._warm = SimpleNamespace(poll=lambda: None, wait=lambda **kw: 0)
    capture._warm_lines.put({'event': 'capture_poll', 'status': status, 'event_count': 0,
                             'window': None, 'metrics': metrics})
    capture._warm_lines.put(None)
    assert capture._wait_warm_poll(1).log != 'capture_poll_completed'


@pytest.mark.parametrize('code', [0, 1, 125, 137])
def test_output_eof_preserves_actual_exit_code(tmp_path, code):
    from types import SimpleNamespace
    capture = DockerCaptureRunner(_config(tmp_path))
    waits = []
    capture._warm = SimpleNamespace(poll=lambda: None,
                                   wait=lambda **kw: waits.append(kw) or code)
    capture._warm_lines.put(None)
    result = capture._wait_warm_poll(1)
    assert result.exit_code == code and result.log == 'capture_worker_exited'
    assert waits == [{'timeout': 5}]


def test_stderr_diagnostic_is_bounded_private_and_not_in_result(tmp_path):
    import io
    from types import SimpleNamespace
    capture = DockerCaptureRunner(_config(tmp_path), diagnostic_directory=tmp_path / 'diagnostics')
    capture._warm_name = 'qdt-owned-test'
    capture._warm = SimpleNamespace(stderr=io.StringIO('x' * 50000 + PASSWORD),
                                   poll=lambda: 1, wait=lambda **kw: 1)
    capture._cleanup_container = lambda *a: None
    capture._pump_stderr()
    assert len(capture._stderr_tail) <= 16384
    capture.close_freshness()
    path = tmp_path / 'diagnostics/qdt-owned-test.private.json'
    data = json.loads(path.read_text())
    assert data['process_exit_code'] == 1 and '[REDACTED]' in data['stderr_tail']
    assert PASSWORD not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_diagnostic_failure_cannot_mask_cleanup_failure_or_keep_worker(tmp_path):
    from types import SimpleNamespace
    target = tmp_path / 'existing-private-directory'
    target.mkdir(mode=0o700)
    link = tmp_path / 'symlink'
    link.symlink_to(target, target_is_directory=True)
    capture = DockerCaptureRunner(_config(tmp_path), diagnostic_directory=link)
    capture._warm_name = 'qdt-owned-test'
    capture._warm = SimpleNamespace(poll=lambda: 1, wait=lambda **kw: 1)
    capture._cleanup_container = lambda *a: (_ for _ in ()).throw(RuntimeError('cleanup failed'))
    with pytest.raises(RuntimeError, match='cleanup failed'):
        capture.close_freshness()
    assert capture._warm is None and capture.diagnostic_error == 'ValueError'
    assert not list(target.iterdir())
