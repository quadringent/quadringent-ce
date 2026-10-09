"""The IBM i qualification driver accepts only generated DML and keeps its secret on stdin."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from quadringent_qualification.config import (
    CaptureConfig, RunConfig, SourceConfig, StorageConfig, WarehouseConfig,
)
from quadringent_qualification.generator import step_plan
from quadringent_qualification.real_source import DockerIbmiSourceDriver
from quadringent_qualification.schema import Column, TableSchema, update_sql


IMAGE = "registry.example/quadringent/capture@sha256:" + "a" * 64


def _config(secret_path: Path, *, allow_dml: bool = True, allow_rotation: bool = True) -> RunConfig:
    table = TableSchema(
        "QUALTEST.QUALIF_ORDERS",
        (
            Column("ORDER_ID", "integer"), Column("LABEL", "varchar", length=40),
            Column("CODE", "char", length=8), Column("AMOUNT", "decimal", precision=11, scale=2),
            Column("EVENT_DATE", "date"), Column("UPDATED_AT", "timestamp"),
            Column("NOTE", "varchar", length=80),
        ),
        "ORDER_ID",
    )
    return RunConfig(
        run_id="qualification-1", table=table,
        source=SourceConfig("ibmi_java", ("QUALTEST",), str(secret_path), "QUALTEST", "QUALJRN",
                            allow_dml=allow_dml, allow_rotation=allow_rotation),
        capture=CaptureConfig(IMAGE), storage=StorageConfig("s3", "qual-bucket", "qualification/1"),
        warehouse=WarehouseConfig("snowflake", "/private/snowflake.json", "QUALDB", "QUALSCHEMA"),
        steps=("seed", "snapshot", "changes1", "capture", "reconcile"),
    )


class RecordingRunner:
    def __init__(self, stdout: str = "SRC_EXEC_TOTAL=1\n") -> None:
        self.stdout = stdout
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return CompletedProcess(command, 0, self.stdout, "")


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"host": "ibmi.example", "user": "QUALUSER", "password": "fixture-only"}))
    path.chmod(0o600)
    return path


def test_generated_sql_runs_with_secret_only_on_stdin(secret_file: Path) -> None:
    runner = RecordingRunner()
    config = _config(secret_file)
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("changes3", config.table).statements[0]

    result = source.execute((statement,))

    assert result.exit_code == 0
    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert IMAGE in command
    assert "fixture-only" not in " ".join(command)
    assert "fixture-only" not in str(kwargs.get("env", {}))
    assert kwargs["input"] == "ibmi.example\nQUALUSER\nfixture-only\n" + statement + "\n"
    assert kwargs["timeout"] <= 120


def test_arbitrary_or_cross_table_dml_is_refused_before_docker(secret_file: Path) -> None:
    runner = RecordingRunner()
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)

    for statement in (
        "DELETE FROM QUALTEST.BUSINESS_ORDERS WHERE ORDER_ID = 1",
        "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1",
        "INSERT INTO QUALTEST.QUALIF_ORDERS VALUES (1); DROP TABLE QUALTEST.BUSINESS_ORDERS",
    ):
        with pytest.raises(ValueError, match="generated"):
            source.execute((statement,))
    assert runner.calls == []


def test_only_three_run_bound_freshness_updates_are_allowed(secret_file: Path) -> None:
    runner = RecordingRunner()
    base = _config(secret_file)
    config = replace(base, steps=(*base.steps, "freshness"))
    source = DockerIbmiSourceDriver(config, runner=runner)
    allowed = update_sql(config.table, 1, {"LABEL": f"Q-{config.run_id}-2"})
    assert source.execute((allowed,)).exit_code == 0
    assert runner.calls[0][1]["input"].endswith(allowed + "\n")

    for forbidden in (
        update_sql(config.table, 2, {"LABEL": f"Q-{config.run_id}-2"}),
        update_sql(config.table, 1, {"LABEL": "Q-foreign-run-2"}),
        update_sql(config.table, 1, {"LABEL": f"Q-{config.run_id}-4"}),
    ):
        with pytest.raises(ValueError, match="generated"):
            source.execute((forbidden,))
    assert len(runner.calls) == 1

    without_freshness = DockerIbmiSourceDriver(base, runner=RecordingRunner())
    with pytest.raises(ValueError, match="generated"):
        without_freshness.execute((allowed,))


def test_source_mutations_require_two_explicit_config_flags(secret_file: Path) -> None:
    runner = RecordingRunner()
    source = DockerIbmiSourceDriver(_config(secret_file, allow_dml=False, allow_rotation=False), runner=runner)
    statement = step_plan("seed", source.config.table).statements[0]
    assert source.execute((statement,)).exit_code != 0
    assert source.rotate().exit_code != 0
    assert runner.calls == []


def test_read_only_source_modes_return_only_expected_protocol_lines(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_TAIL={"JOURNAL_RECEIVER_NAME":"QUALJRN0001","LAST_SEQUENCE_NUMBER":"8"}\n'
        'SRC_TAIL_COUNT=1\n'
        'unrelated diagnostic that must not enter the result\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    result = source.tail()
    assert result.exit_code == 0
    assert len(result.parsed("SRC_TAIL")) == 1
    assert len(result.checks) == 2
    assert "tail" in runner.calls[0][0]


def test_inspect_returns_a_single_journal_receipt(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_JOURNAL={"JOURNAL_LIBRARY":"QUALTEST","JOURNAL_NAME":"QUALJRN"}\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file, allow_dml=False), runner=runner)
    receipt = source.inspect()
    assert receipt.exit_code == 0
    assert receipt.parsed("SRC_JOURNAL") == [
        {"JOURNAL_LIBRARY": "QUALTEST", "JOURNAL_NAME": "QUALJRN"}
    ]
    runner.stdout += 'SRC_JOURNAL={"JOURNAL_LIBRARY":"OTHER","JOURNAL_NAME":"OTHER"}\n'
    assert source.inspect().exit_code != 0


def test_tail_requires_matching_count_receipt(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_TAIL={"JOURNAL_RECEIVER_NAME":"QUALJRN0001","LAST_SEQUENCE_NUMBER":"8"}\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    assert source.tail().exit_code != 0
    runner.stdout += 'SRC_TAIL_COUNT=2\n'
    assert source.tail().exit_code != 0


def test_secret_file_must_be_private_and_image_must_be_immutable(secret_file: Path) -> None:
    secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        DockerIbmiSourceDriver(_config(secret_file))
    secret_file.chmod(0o600)
    config = _config(secret_file)
    with pytest.raises(ValueError, match="digest"):
        DockerIbmiSourceDriver(replace(config, capture=CaptureConfig("capture:latest")))


def test_secret_file_symlink_is_refused(secret_file: Path, tmp_path: Path) -> None:
    link = tmp_path / "linked-source.json"
    link.symlink_to(secret_file)
    with pytest.raises(ValueError, match="unreadable or invalid"):
        DockerIbmiSourceDriver(_config(link))


def test_keychain_secret_file_never_enters_docker_arguments(monkeypatch: pytest.MonkeyPatch,
                                                               secret_file: Path) -> None:
    security_calls: list[list[str]] = []

    def fake_security(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        security_calls.append(command)
        return CompletedProcess(command, 0, "fixture-only\n", "")

    monkeypatch.setattr("quadringent_qualification.real_source.subprocess.run", fake_security)
    runner = RecordingRunner(stdout="SRC_EXEC_TOTAL=1\n")
    config = _config(secret_file)
    source_config = replace(
        config.source, connection_secret_file="keychain:" + "Qualification/Test",
        host="ibmi.example", user="QUALUSER",
    )
    config = replace(config, source=source_config,
                     capture=CaptureConfig("sha256:" + "a" * 64))
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("changes3", config.table).statements[0]
    assert source.execute((statement,)).exit_code == 0
    assert security_calls == [["security", "find-generic-password", "-s", "Qualification/Test", "-w"]]
    docker_command, docker_options = runner.calls[0]
    assert "fixture-only" not in " ".join(docker_command)
    assert docker_options["input"] == "ibmi.example\nQUALUSER\nfixture-only\n" + statement + "\n"


def test_keychain_source_requires_host_and_user(secret_file: Path) -> None:
    config = _config(secret_file)
    source_config = replace(config.source, connection_secret_file="keychain:" + "Qualification/Test")
    with pytest.raises(ValueError, match="host and user"):
        DockerIbmiSourceDriver(replace(config, source=source_config))


def test_read_only_inspection_does_not_require_generator_columns(secret_file: Path) -> None:
    config = _config(secret_file, allow_dml=False)
    narrow_table = TableSchema("QUALTEST.QDC_TAIL", (Column("ID", "integer"),), "ID")
    config = replace(config, table=narrow_table)
    runner = RecordingRunner(
        'SRC_JOURNAL={"JOURNAL_LIBRARY":"QUALTEST","JOURNAL_NAME":"QUALJRN"}\n'
    )
    source = DockerIbmiSourceDriver(config, runner=runner)
    assert source.inspect().exit_code == 0
    assert len(runner.calls) == 1


def test_successful_exit_without_source_receipt_is_not_accepted(secret_file: Path) -> None:
    runner = RecordingRunner(stdout="")
    config = _config(secret_file)
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("seed", config.table).statements[0]
    assert source.execute((statement,)).exit_code != 0
    assert source.tail().exit_code != 0
    assert source.dump().exit_code != 0
    assert source.row_positions(("QUALJRN0001", 1)).exit_code != 0
    assert source.rotate().exit_code != 0


def test_zero_row_dump_and_zero_position_oracle_need_explicit_receipts(secret_file: Path) -> None:
    runner = RecordingRunner(stdout="SRC_ROW_COUNT=0\n")
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    assert source.dump().exit_code == 0
    runner.stdout = 'SRC_RECEIVER={"JOURNAL_RECEIVER_NAME":"QUALJRN0001"}\nSRC_ROWPOS_COUNT=0\n'
    assert source.row_positions(("QUALJRN0001", 1)).exit_code == 0


def test_rotation_preserves_actual_start_ack_once(secret_file):
    started = "2026-10-05T01:00:00Z"
    receipt = {"started_utc": started, "ack_utc": "2026-10-05T01:00:01Z",
               "attempts": 1, "acknowledged": True, "sequence_option": "CONT", "journal_library": "QUALTEST", "journal_name": "QUALJRN"}
    output = "SRC_ROTATION_STARTED=" + started + "\nSRC_ROTATION_ACK=" + json.dumps(receipt) + "\nSRC_ROTATED=true\n"
    runner = RecordingRunner(output)
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    result = source.rotate()
    assert result.exit_code == 0
    assert result.checks == tuple(output.splitlines())
    assert len(runner.calls) == 1


@pytest.mark.parametrize("change", [
    {"journal_library": "FOREIGN"}, {"journal_name": "FOREIGN"}, {"attempts": 2}, {"attempts": True}, {"acknowledged": False}, {"sequence_option": "RESET"},
    {"started_utc": "2026-10-05T01:00:02Z"}, {"ack_utc": "2026-10-05T00:00:00Z"},
    {"ack_utc": "2026-10-05T01:00:01"},
])
def test_rotation_refuses_ambiguous_ack_without_retry(secret_file, change):
    started = "2026-10-05T01:00:00Z"
    receipt = {"started_utc": started, "ack_utc": "2026-10-05T01:00:01Z",
               "attempts": 1, "acknowledged": True, "sequence_option": "CONT", "journal_library": "QUALTEST", "journal_name": "QUALJRN", **change}
    runner = RecordingRunner("SRC_ROTATION_STARTED=" + started + "\nSRC_ROTATION_ACK=" + json.dumps(receipt) + "\nSRC_ROTATED=true\n")
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    assert source.rotate().exit_code != 0
    assert len(runner.calls) == 1


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'interrupt'])
@pytest.mark.parametrize('absence', ['Error: No such object', 'error: no such object', 'error: no such container'])
def test_source_container_is_named_and_absent_before_failure_returns(secret_file, failure, absence):
    import subprocess
    from uuid import UUID
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == 'run':
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 1)
            if failure == 'oserror':
                raise OSError('fixture transport failure')
            raise KeyboardInterrupt
        if command[1] == 'rm':
            return CompletedProcess(command, 0, '', '')
        return CompletedProcess(command, 1, '[]', f'{absence}: {command[-1]}')
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    statement = step_plan('changes3', source.config.table).statements[0]
    if failure == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            source.execute((statement,))
    else:
        assert source.execute((statement,)).exit_code == 124
    command = calls[0][0]
    name = command[command.index('--name') + 1]
    UUID(name.removeprefix('qdt-qual-source-'))
    assert '--pull=never' in command
    assert command[command.index('--network') + 1] == 'host'
    assert [c for c, _ in calls[1:]] == [
        ['docker', 'rm', '--force', '--volumes', name],
        ['docker', 'container', 'inspect', name],
    ]
    assert sum(c[1] == 'run' for c, _ in calls) == 1
    assert all('input' not in opts for _, opts in calls[1:])


@pytest.mark.parametrize('inspection', ['running', 'unavailable'])
def test_source_cleanup_without_confirmed_absence_fails_closed(secret_file, inspection):
    import subprocess
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if command[1] == 'run':
            raise subprocess.TimeoutExpired(command, 1)
        if command[1] == 'rm':
            return CompletedProcess(command, 0, '', '')
        if inspection == 'unavailable':
            raise OSError('fixture daemon unavailable')
        return CompletedProcess(command, 0, '[{"State":{"Running":true}}]', '')
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    with pytest.raises(RuntimeError, match='cleanup could not be verified'):
        source.tail()
    assert sum(c[1] == 'run' for c in calls) == 1


@pytest.mark.parametrize('absent', [False, True])
def test_nonzero_docker_exit_requires_source_container_absence(secret_file, absent):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if command[1] == 'run':
            return CompletedProcess(command, 1, '', 'fixture client disconnect')
        if command[1] == 'rm':
            return CompletedProcess(command, 0, '', '')
        return (CompletedProcess(command, 1, '', 'No such container') if absent
                else CompletedProcess(command, 0, '[{"State":{"Running":true}}]', ''))
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    statement = step_plan('changes3', source.config.table).statements[0]
    if absent:
        assert source.execute((statement,)).exit_code == 1
    else:
        with pytest.raises(RuntimeError, match='cleanup could not be verified'):
            source.execute((statement,))
    name = calls[0][calls[0].index('--name') + 1]
    assert calls[1:] == [['docker', 'rm', '--force', '--volumes', name],
                         ['docker', 'container', 'inspect', name]]
    assert sum(c[1] == 'run' for c in calls) == 1


def _warm_fixture(tmp_path, *, acknowledge=True):
    import queue
    from io import StringIO
    secret = tmp_path / 'source.json'
    secret.write_text(json.dumps({'host': 'ibmi.example', 'user': 'QUALUSER', 'password': 'fixture-only'}))
    secret.chmod(0o600)
    config = replace(_config(secret), run_id='8d291bee-a837-48fb-aba6-f04f643e13c4',
                     steps=('freshness',))
    class Process:
        code = None
        def __init__(self):
            self.output = queue.Queue()
            self.events = []
            owner = self
            class Input(StringIO):
                def write(self, value):
                    owner.events.append(value)
                    if value.startswith('FRESHNESS_EXEC=') and acknowledge:
                        owner.output.put('SRC_FRESHNESS_ACK=' + value.split('=')[1])
                    elif 'fixture-only' in value:
                        owner.output.put('SRC_FRESHNESS_READY=' + config.run_id + '\n')
                    return len(value)
                def close(self):
                    owner.output.put(None)
                    super().close()
            self.stdin = Input()
            self.stdout = iter(self.output.get, None)
        def poll(self):
            return self.code
        def terminate(self):
            self.events.append('TERM')
            self.code = -15
        def kill(self):
            self.events.append('KILL')
            self.code = -9
        def wait(self, *, timeout):
            self.events.append(('wait', timeout))
            return self.code
    process = Process()
    calls = []
    def runner(command, **kw):
        calls.append(command)
        if command[1:3] == ['container', 'inspect']:
            return CompletedProcess(command, 1, '', 'No such container')
        return CompletedProcess(command, 0, '', '')
    def factory(command, **kw):
        calls.append(command)
        assert 'fixture-only' not in str(command) and kw['stderr'] == -3
        return process
    return DockerIbmiSourceDriver(config, runner=runner, process_factory=factory), process, calls


def test_prepared_source_connects_without_dml_then_exact_three_dispatches(tmp_path):
    from quadringent_qualification.generator import freshness_markers
    driver, process, calls = _warm_fixture(tmp_path)
    assert driver.prepare_freshness().checks == ('SRC_CONNECTION_READY=1',)
    assert len(process.events) == 1 and 'FRESHNESS_EXEC=' not in process.events[0]
    for marker in freshness_markers(driver.config.run_id):
        assert driver.execute((update_sql(driver.config.table, 1, {'LABEL': marker}),)).exit_code == 0
    with pytest.raises(ValueError):
        driver.execute((update_sql(driver.config.table, 1, {'LABEL': freshness_markers(driver.config.run_id)[0]}),))
    driver.close_freshness()
    assert [c[1] for c in calls] == ['run', 'rm', 'container']
    assert calls[0][calls[0].index('--name') + 1].startswith('qdt-qual-source-')
    assert process.stdin.closed and driver._warm is None


def test_prepared_source_rejects_out_of_order_and_dead_worker_before_dispatch(tmp_path):
    from quadringent_qualification.generator import freshness_markers
    driver, process, _ = _warm_fixture(tmp_path)
    driver.prepare_freshness()
    for marker in (freshness_markers(driver.config.run_id)[1],):
        with pytest.raises(ValueError):
            driver.execute((update_sql(driver.config.table, 1, {'LABEL': marker}),))
    process.code = 0
    with pytest.raises(ValueError):
        driver.execute((update_sql(driver.config.table, 1, {'LABEL': freshness_markers(driver.config.run_id)[0]}),))
    assert len(process.events) == 1
    driver.close_freshness()


def test_ambiguous_source_timeout_cannot_retry_and_still_cleans(tmp_path, monkeypatch):
    import queue
    from quadringent_qualification.generator import freshness_markers
    driver, process, _ = _warm_fixture(tmp_path, acknowledge=False)
    driver.prepare_freshness()
    monkeypatch.setattr(driver._warm_lines, 'get', lambda **kw: (_ for _ in ()).throw(queue.Empty()))
    statement = update_sql(driver.config.table, 1, {'LABEL': freshness_markers(driver.config.run_id)[0]})
    assert driver.execute((statement,)).exit_code == 124
    with pytest.raises(ValueError):
        driver.execute((statement,))
    driver.close_freshness()
    assert process.events.count('FRESHNESS_EXEC=1\n') == 1
    assert 'TERM' in process.events and process.stdin.closed


def test_source_cleanup_escalates_kill_and_confirms_absence_on_timeout(tmp_path):
    import subprocess
    driver, process, calls = _warm_fixture(tmp_path)
    driver.prepare_freshness()
    original_wait = process.wait
    def wait(*, timeout):
        if timeout == 10:
            raise subprocess.TimeoutExpired('owned-source', timeout)
        return original_wait(timeout=timeout)
    process.wait = wait
    driver.close_freshness()
    assert process.events[-3:] == ['TERM', 'KILL', ('wait', 5)]
    assert calls[-1][1:3] == ['container', 'inspect'] and driver._warm is None


def test_source_cleanup_failure_is_visible_and_never_keeps_warm_reference(tmp_path):
    driver, _, _ = _warm_fixture(tmp_path)
    driver.prepare_freshness()
    driver._cleanup_container = lambda name: (_ for _ in ()).throw(RuntimeError('cleanup unconfirmed'))
    with pytest.raises(RuntimeError, match='cleanup unconfirmed'):
        driver.close_freshness()
    assert driver._warm is None


def test_close_before_source_prepare_is_safe_and_does_not_touch_docker(tmp_path):
    driver, process, calls = _warm_fixture(tmp_path)
    driver.close_freshness()
    assert calls == [] and process.events == []
    assert driver._warm is None and driver._warm_name is None and driver._warm_thread is None


def test_source_process_factory_oserror_preserves_failure_and_cleans_exact_name(tmp_path):
    driver, process, calls = _warm_fixture(tmp_path)
    def unavailable(*args, **kwargs):
        raise OSError('private-process-failure')
    driver._process_factory = unavailable
    result = driver.prepare_freshness()
    name = driver._warm_name
    assert result.exit_code == 124 and result.checks == ()
    driver.close_freshness()
    assert calls == [['docker', 'rm', '--force', '--volumes', name],
                     ['docker', 'container', 'inspect', name]]
    assert driver._warm is None and driver._warm_name is None and driver._warm_thread is None
    assert process.events == [] and 'private-process-failure' not in repr(result)
