"""``quadringent local start``: ordering, stop conditions, idempotent restart, stop and reset."""
from __future__ import annotations

import json

import pytest

from local_cli_support import RESULT, Cli, DockerStub, probe_output, result
from quadringent.local import dockerplan as plan
from quadringent.local.config import VOLUME_ID_LABEL
from quadringent.local.orchestrator import Docker, InstanceLock, load_run_state

ORDER = ["fs-probe", "probe", "discover", "boundary", "admit", "copy", "capture", "loader"]


def init(tmp_path, runner=None, *extra):
    cli = Cli(tmp_path, runner=runner)
    assert cli.init(*extra) == 0
    return cli


def test_start_runs_the_steps_in_order_and_records_the_stages(tmp_path):
    cli = init(tmp_path)
    assert cli.start() == 0
    assert cli.runner.roles() == ORDER
    state = load_run_state(cli.paths)
    assert set(state["stages"]) >= {"identity", "probe", "time_zone", "table", "admission", "copy"}
    assert state["stages"]["time_zone"] == "IBM:QP0100CET"
    assert state["stages"]["copy"]["rows"] == 3
    assert state["stages"]["table"]["key_columns"] == ["ID"]
    assert "Running." in cli.out.getvalue() and "copied 3 rows" in cli.out.getvalue()
    # the admission handed to the copy container is a private file mounted read-only
    assert oct(cli.paths.admission.stat().st_mode & 0o777) == "0o600"
    assert json.loads(cli.paths.admission.read_text())["run_id"]


def test_the_loader_receives_the_bound_table_set_and_the_capture_its_identity(tmp_path):
    cli = init(tmp_path)
    cli.start()
    entry = json.loads(cli.env_file("loader")["QUADRINGENT_LOADER_TABLE_SET_JSON"])[0]
    admission = json.loads(cli.paths.admission.read_text())
    assert entry["table_id"] == cli.config().table_id and entry["pipeline_id"] == cli.config().pipeline_id
    assert entry["run_id"] == admission["run_id"] and entry["evidence_key"] == admission["evidence_key"]
    assert entry["key_columns"] == ["ID"] and [c["name"] for c in entry["columns"]] == ["ID", "NAME", "CREATED"]
    capture = cli.env_file("capture")
    assert capture["AS400_STREAM_KEY"] == admission["stream_key"]
    assert capture["AS400_RAW_PREFIX"] == "raw/orders/journal"
    assert capture["AS400_JOURNAL_LIBRARY"] == "QDCLIB" and capture["AS400_JOURNAL_NAME"] == "QSQJRN"
    assert capture["AS400_BOOTSTRAP_SEQUENCE"] == "4200"


def test_a_second_start_resumes_without_probe_discovery_admission_or_copy(tmp_path):
    cli = init(tmp_path)
    assert cli.start() == 0
    first = len(cli.runner.runs())
    assert cli.start() == 0
    again = [role for role in cli.runner.roles()[first:]]
    assert again == ["fs-probe"]  # the file system gate is checked on every start; services already run
    assert "already running" in cli.out.getvalue()
    # a stopped capture is started again from the recorded stages, still without a new admission or copy
    cli.runner.states.clear()
    assert cli.start() == 0
    assert cli.runner.roles()[first + 1:] == ["fs-probe", "capture", "loader"]


def test_a_failed_copy_is_retried_alone_after_the_admission_was_recorded(tmp_path):
    state = {"fail": True}

    def copy(argv):
        if state["fail"]:
            return result(argv, "", 1, "quadringent_local_error=copy_failed: snapshot interrupted\n")
        return RESULT + json.dumps({"evidence_key": "raw/x/evidence/y.json", "rows": 7}) + "\n"

    cli = init(tmp_path, DockerStub(copy=copy))
    assert cli.start() == 1
    assert "the initial copy failed: quadringent_local_error=copy_failed: snapshot interrupted" in cli.err.getvalue()
    assert "capture" not in cli.runner.roles()
    stages = load_run_state(cli.paths)["stages"]
    assert "admission" in stages and "copy" not in stages
    state["fail"] = False
    count = len(cli.runner.runs())
    assert cli.start() == 0
    assert cli.runner.roles()[count:] == ["fs-probe", "copy", "capture", "loader"]
    assert load_run_state(cli.paths)["stages"]["copy"]["rows"] == 7


def test_an_authentication_failure_stops_everything_without_retry(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(auth=False, auth_detail="authentication rejected")
    cli = init(tmp_path, runner)
    assert cli.start() == 1
    error = cli.err.getvalue()
    assert "sign-on refused" in error and "authentication rejected" in error and "nothing will be retried" in error
    assert cli.runner.roles() == ["fs-probe", "probe"]  # exactly one sign-on attempt, nothing after it
    assert "admission" not in load_run_state(cli.paths)["stages"]
    # running start again is a new, explicit operator action, never an automatic retry
    assert cli.start() == 1 and cli.runner.roles().count("probe") == 2


def test_an_unknown_authority_is_never_trusted_and_the_fingerprint_is_shown(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(trust="unknown")
    cli = init(tmp_path, runner)
    assert cli.start() == 1
    assert "not trusted" in cli.err.getvalue() and "--ibmi-ca-file" in cli.err.getvalue() and ("ab" * 32) in cli.err.getvalue()
    assert cli.runner.roles() == ["fs-probe", "probe"]


def test_an_unreachable_server_stops_at_the_probe(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(network=False, tls=False, auth=False)
    cli = init(tmp_path, runner)
    assert cli.start() == 1 and "unreachable" in cli.err.getvalue()
    assert cli.runner.roles() == ["fs-probe", "probe"]


def test_a_missing_time_zone_asks_for_one_instead_of_guessing(tmp_path):
    runner = DockerStub()
    runner.probe = probe_output(qtimzon="QUNKNOWN", zone=None)
    cli = init(tmp_path, runner)
    assert cli.start() == 1 and "--source-time-zone" in cli.err.getvalue()
    assert cli.runner.roles() == ["fs-probe", "probe"]
    explicit = init(tmp_path / "explicit", runner, "--source-time-zone", "Europe/Paris")
    assert explicit.start() == 0
    assert explicit.env_file("capture")["AS400_SOURCE_TIME_ZONE"] == "Europe/Paris"


def test_an_unsupported_file_system_stops_before_any_ibmi_sign_on(tmp_path):
    bad = RESULT + json.dumps({"ok": False, "checks": {"flock": {"ok": False, "detail": "no conflict"}}}) + "\n"
    cli = init(tmp_path, DockerStub(fs_probe=result(("x",), bad, 1)))
    assert cli.start() == 1
    assert "unsupported file system" in cli.err.getvalue() and "flock" in cli.err.getvalue()
    assert "Docker Desktop" in cli.err.getvalue() and "APFS" not in cli.err.getvalue()
    assert cli.runner.roles() == ["fs-probe"]


def test_confirmation_is_required_without_yes_and_declining_copies_nothing(tmp_path):
    cli = init(tmp_path)
    assert cli.invoke("start") == 1  # stdin is not a terminal: no silent consent
    assert "pass --yes" in cli.out.getvalue()
    assert cli.runner.roles() == ["fs-probe", "probe", "discover"]
    assert cli.invoke("start", confirm=lambda question: False) == 1
    assert "boundary" not in cli.runner.roles()
    assert cli.invoke("start", confirm=lambda question: True) == 0
    assert "boundary" in cli.runner.roles()


def test_the_summary_names_what_is_read_and_written_and_the_cost_exposure(tmp_path):
    cli = init(tmp_path)
    cli.start()
    summary = cli.out.getvalue()
    for expected in ("read-only", "CLEAR TEXT", "auto-suspend 60 s", "QDCLIB/ORDERS", "TLS with certificate verification",
                     cli.config().warehouse, "RAW (history)", "CURATED (mirror)"):
        assert expected in summary, expected
    for expected in ("billed while running", "including idle time", "each resume has a 60 s minimum"):
        assert expected in summary
    assert "bills while loading" not in summary


def test_a_changed_configuration_cannot_resume_under_a_recorded_admission(tmp_path):
    cli = init(tmp_path)
    cli.start()
    config = json.loads(cli.paths.config.read_text())
    config["table"]["name"] = "OTHER"
    cli.paths.config.write_text(json.dumps(config))
    assert cli.start() == 1 and "configuration changed" in cli.err.getvalue()


def test_two_commands_cannot_run_at_once(tmp_path):
    cli = init(tmp_path)
    with InstanceLock(cli.paths.lock):
        assert cli.start() == 1
    assert "another quadringent local command" in cli.err.getvalue()
    assert cli.runner.runs() == []


def test_docker_missing_or_stopped_is_reported(tmp_path):
    cli = init(tmp_path)
    cli.runner.available_tools = frozenset()
    assert cli.start() == 1 and "docker is not installed" in cli.err.getvalue()


def test_reset_needs_yes_stops_containers_and_removes_data_and_run_state_only(tmp_path):
    cli = init(tmp_path)
    cli.start()
    secrets_before = cli.paths.ibmi_password.read_text()
    (cli.paths.data / "objects").mkdir()
    (cli.paths.data / "objects" / "batch.json").write_text("{}")
    assert cli.invoke("reset") == 2 and (cli.paths.data / "objects" / "batch.json").exists()
    assert cli.invoke("reset", "--yes") == 0
    assert not (cli.paths.data / "objects").exists() and not cli.paths.run_json.exists()
    assert cli.paths.marker.exists() and json.loads(cli.paths.marker.read_text())["label"] == cli.config().label
    assert cli.paths.ibmi_password.read_text() == secrets_before and cli.paths.config.exists()
    assert cli.runner.states == {}
    assert "Snowflake tables are not touched" in cli.out.getvalue()
    assert cli.start() == 0 and load_run_state(cli.paths)["stages"]["copy"]  # a fresh instance of the run


def test_reset_refuses_a_data_directory_without_the_marker(tmp_path):
    cli = init(tmp_path)
    cli.paths.marker.unlink()
    assert cli.invoke("reset", "--yes") == 1 and "marker" in cli.out.getvalue()
    assert cli.paths.data.exists()


def test_status_reports_containers_stages_and_the_console_snapshot_without_claiming_delivery(tmp_path):
    cli = init(tmp_path)
    cli.start()
    snapshot = {"generated_at": "2026-10-13T10:00:00Z", "run": {"state": "RUNNING", "stopped_because": None},
                "position": {"checkpoint": "RCV:4300"}, "lag": {"current": {"known": True, "value": 12}},
                "counters": {"events_published": {"known": True, "value": 40}, "errors": {"known": True, "value": 0},
                             "polls": {"known": True, "value": 9}}}
    (cli.paths.data / "console").mkdir(exist_ok=True)
    cli.paths.console_snapshot.write_text(json.dumps(snapshot))
    assert cli.invoke("status", "--json") == 0
    document = json.loads(cli.out.getvalue())
    assert document["containers"] == {"capture": "running", "loader": "running"}
    assert document["stages"] == {"probe": True, "table": True, "admission": True, "copy": True}
    assert document["capture"]["lag_sequences"] == 12 and document["capture"]["events_published"] == 40
    assert "not measured" in document["delivery"]
    assert cli.invoke("status", "--logs") == 0
    text = cli.out.getvalue()
    assert "capture  running" in text and "lag 12" in text and "--- loader log" in text
    assert any(argv[:3] == ("docker", "logs", "--tail") for argv, _, _ in cli.runner.invocations)


def container_labels(cli, role, owner=None):
    return {plan.LABEL: cli.name, VOLUME_ID_LABEL: owner or cli.config().label, f"{plan.LABEL}.role": role}


def destructive_calls(runner, since=0):
    return [argv for argv, _, _ in runner.invocations[since:]
            if argv[:2] in (("docker", "stop"), ("docker", "rm"))]


@pytest.mark.parametrize("command", [("stop",), ("reset", "--yes")])
@pytest.mark.parametrize("foreign_labels", [None, {}, {plan.LABEL: "demo"}])
def test_a_mixed_container_batch_is_refused_before_touching_any_owned_container(tmp_path, command, foreign_labels):
    cli = init(tmp_path)
    cli.runner.add_container(plan.container_name(cli.name, "capture"), container_labels(cli, "capture"))
    labels = container_labels(cli, "loader", "qdt-foreign") if foreign_labels is None else foreign_labels
    cli.runner.add_container(plan.container_name(cli.name, "loader"), labels)
    before = len(cli.runner.invocations)
    assert cli.invoke(*command) == 1
    assert destructive_calls(cli.runner, before) == []
    assert set(cli.runner.states.values()) == {"running"}
    assert cli.paths.data.exists() and cli.paths.config.exists()


@pytest.mark.parametrize("foreign_labels", [None, {}, {plan.LABEL: "demo"}])
def test_replacing_a_foreign_or_unlabelled_container_is_refused(tmp_path, foreign_labels):
    cli = init(tmp_path)
    labels = container_labels(cli, "fs-probe", "qdt-foreign") if foreign_labels is None else foreign_labels
    name = plan.container_name(cli.name, "fs-probe")
    identity = cli.runner.add_container(name, labels, state="exited")
    docker = Docker(cli.runner, cli.config(), cli.paths, uid=1000, gid=1000)
    before = len(cli.runner.invocations)
    response = docker.run(plan.fs_probe_spec(cli.config(), cli.paths))
    assert not response.ok and "refusing" in response.stderr
    assert destructive_calls(cli.runner, before) == [] and cli.runner.runs() == []
    assert cli.runner.containers[name]["Id"] == identity


def test_start_checks_all_service_owners_before_replacing_any_helper(tmp_path):
    cli = init(tmp_path)
    cli.runner.add_container(plan.container_name(cli.name, "fs-probe"), container_labels(cli, "fs-probe"), state="exited")
    cli.runner.add_container(plan.container_name(cli.name, "loader"), container_labels(cli, "loader", "qdt-foreign"))
    before = len(cli.runner.invocations)
    assert cli.start() == 1
    assert destructive_calls(cli.runner, before) == [] and cli.runner.runs() == []
    assert set(cli.runner.states.values()) == {"running", "exited"}


@pytest.mark.parametrize("initial_loader", [True, False])
@pytest.mark.parametrize("foreign_owner", [True, False])
def test_a_container_replaced_during_batch_preflight_refuses_the_whole_batch(tmp_path, initial_loader, foreign_owner):
    cli = init(tmp_path)
    for role in plan.ROLES_SERVICE:
        if role != "loader" or initial_loader:
            cli.runner.add_container(plan.container_name(cli.name, role), container_labels(cli, role))
    run = cli.runner.run
    loader = plan.container_name(cli.name, "loader")
    reads = 0

    def race(argv, **kwargs):
        nonlocal reads
        if tuple(argv[:3]) == ("docker", "container", "inspect") and argv[-1] == loader:
            reads += 1
            if reads == 2:
                cli.runner.add_container(loader, container_labels(cli, "loader", "qdt-foreign" if foreign_owner else None))
        return run(argv, **kwargs)

    cli.runner.run = race
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 1
    assert destructive_calls(cli.runner, before) == []
    assert set(cli.runner.states.values()) == {"running"}


def test_stop_and_remove_use_the_inspected_id_if_a_name_is_reassigned(tmp_path):
    cli = init(tmp_path)
    name = plan.container_name(cli.name, "capture")
    identity = cli.runner.add_container(name, container_labels(cli, "capture"))
    run = cli.runner.run

    def race(argv, **kwargs):
        answer = run(argv, **kwargs)
        if tuple(argv[:2]) == ("docker", "stop"):
            cli.runner.add_container(name, container_labels(cli, "capture", "qdt-foreign"))
        return answer

    cli.runner.run = race
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 1
    assert all(argv[-1] == identity for argv in destructive_calls(cli.runner, before))
    assert cli.runner.states[name] == "running" and cli.runner.containers[name]["Id"] != identity


def test_replacement_never_removes_a_foreign_container_reusing_the_inspected_name(tmp_path):
    cli = init(tmp_path)
    name = plan.container_name(cli.name, "fs-probe")
    identity = cli.runner.add_container(name, container_labels(cli, "fs-probe"), state="exited")
    run = cli.runner.run

    def race(argv, **kwargs):
        if tuple(argv[:2]) == ("docker", "rm"):
            cli.runner.add_container(name, container_labels(cli, "fs-probe", "qdt-foreign"))
        return run(argv, **kwargs)

    cli.runner.run = race
    before = len(cli.runner.invocations)
    docker = Docker(cli.runner, cli.config(), cli.paths, uid=1000, gid=1000)
    response = docker.run(plan.fs_probe_spec(cli.config(), cli.paths))
    assert not response.ok and cli.runner.runs() == []
    assert destructive_calls(cli.runner, before) == [("docker", "rm", "-f", identity)]
    assert cli.runner.states[name] == "running" and cli.runner.containers[name]["Id"] != identity


@pytest.mark.parametrize("payload", ["not json", "[]", '[{"Id":"short"}]'])
def test_an_unreadable_container_identity_refuses_stop_without_destructive_calls(tmp_path, payload):
    cli = init(tmp_path)
    run = cli.runner.run

    def unreadable(argv, **kwargs):
        if tuple(argv[:3]) == ("docker", "container", "inspect"):
            return result(argv, payload)
        return run(argv, **kwargs)

    cli.runner.run = unreadable
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 1
    assert destructive_calls(cli.runner, before) == []


def test_owned_container_replacement_removes_only_its_inspected_id(tmp_path):
    cli = init(tmp_path)
    name = plan.container_name(cli.name, "fs-probe")
    identity = cli.runner.add_container(name, container_labels(cli, "fs-probe"), state="exited")
    docker = Docker(cli.runner, cli.config(), cli.paths, uid=1000, gid=1000)
    before = len(cli.runner.invocations)
    assert docker.run(plan.fs_probe_spec(cli.config(), cli.paths)).ok
    assert destructive_calls(cli.runner, before) == [("docker", "rm", "-f", identity)]


@pytest.mark.parametrize("flag,role", [("--signon", "probe"), ("--discover", "discover"), ("--snowflake", "verify")])
def test_check_reports_a_foreign_helper_without_contacting_its_remote_system(tmp_path, flag, role):
    cli = init(tmp_path)
    name = plan.container_name(cli.name, role)
    cli.runner.add_container(name, container_labels(cli, role, "qdt-foreign"))
    before = len(cli.runner.invocations)
    assert cli.invoke("check", flag, "--json") == 1
    report = json.loads(cli.out.getvalue())
    assert any("refusing" in item["summary"] for item in report["results"])
    assert destructive_calls(cli.runner, before) == [] and cli.runner.runs() == []


@pytest.mark.parametrize("error", ["permission denied", "No such container: other-name", "unexpected daemon failure"])
def test_an_inspection_failure_is_never_treated_as_container_absence(tmp_path, error):
    cli = init(tmp_path)
    run = cli.runner.run

    def denied(argv, **kwargs):
        if tuple(argv[:3]) == ("docker", "container", "inspect"):
            return result(argv, returncode=1, stderr=error)
        return run(argv, **kwargs)

    cli.runner.run = denied
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 1
    assert destructive_calls(cli.runner, before) == []


def test_service_containers_carry_the_unique_instance_owner_label(tmp_path):
    cli = init(tmp_path)
    assert cli.start() == 0
    for role in plan.ROLES_SERVICE:
        labels = cli.runner.containers[plan.container_name(cli.name, role)]["Config"]["Labels"]
        assert labels == container_labels(cli, role)


def test_a_matching_owner_label_does_not_adopt_a_container_of_the_wrong_role(tmp_path):
    cli = init(tmp_path)
    cli.runner.add_container(plan.container_name(cli.name, "capture"), container_labels(cli, "loader"))
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 1
    assert destructive_calls(cli.runner, before) == []


@pytest.mark.parametrize("prefix", ["Error: ", "Error response from daemon: "])
def test_only_an_explicit_missing_container_readback_allows_an_empty_stop(tmp_path, prefix):
    cli = init(tmp_path)
    run = cli.runner.run

    def absent(argv, **kwargs):
        if tuple(argv[:3]) == ("docker", "container", "inspect"):
            return result(argv, returncode=1, stderr=prefix + "No such container: " + argv[-1])
        return run(argv, **kwargs)

    cli.runner.run = absent
    before = len(cli.runner.invocations)
    assert cli.invoke("stop") == 0
    assert destructive_calls(cli.runner, before) == []
