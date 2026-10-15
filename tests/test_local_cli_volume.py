"""Data kept in a Docker named volume (Docker Desktop): plans, helpers, status, reset, detection."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_cli_support import RESULT, Cli, DockerStub, option, result, role_of
from quadringent.local import config as cfg
from quadringent.local import dockerplan as plan
from quadringent.local.datalocation import default_data_location
from quadringent.local.orchestrator import load_run_state

VOLUME = "qdt-local-demo-data"
LABEL, ID_LABEL = "io.quadringent.local", "io.quadringent.local.id"
HARDENING = ("--init", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges")
VOLUME_ROLES = ("init-data", "fs-probe", "probe", "discover", "boundary", "admit", "copy", "capture", "loader")


def volume_cli(tmp_path, *init_args, **kwargs):
    cli = Cli(tmp_path, **kwargs)
    assert cli.init("--data-location", "volume", *init_args) == 0, cli.err.getvalue()
    return cli


def started(tmp_path, *init_args):
    cli = volume_cli(tmp_path, *init_args)
    assert cli.start() == 0, cli.err.getvalue()
    return cli


def volume_calls(cli, verb=None):
    return [argv for argv, _, _ in cli.runner.invocations
            if argv[:2] == ("docker", "volume") and (verb is None or argv[2] == verb)]


def role(argv):
    return role_of(argv)


# -- detection and the setting ---------------------------------------------------------------------------------


@pytest.mark.parametrize("system, platform, expected", [
    ("Docker Desktop", "linux", "volume"),
    ("Docker Desktop", "darwin", "volume"),
    ("Ubuntu 24.04.1 LTS", "darwin", "volume"),
    ("Ubuntu 24.04.1 LTS", "linux", "bind"),
    ("Debian GNU/Linux 12", "linux", "bind"),
])
def test_default_data_location_follows_docker_desktop_or_the_host_os(system, platform, expected):
    runner = DockerStub()
    runner.operating_system = system
    location, reason = default_data_location(runner, platform)
    assert location == expected and reason
    assert ("docker", "info", "--format", "{{.OperatingSystem}}") in [argv for argv, _, _ in runner.invocations]


def test_a_missing_or_failing_docker_falls_back_to_the_host_os():
    runner = DockerStub()
    runner.available_tools = frozenset()
    assert default_data_location(runner, "linux")[0] == "bind"
    assert default_data_location(runner, "darwin")[0] == "volume"
    assert runner.invocations == []  # no docker, nothing run
    failing = DockerStub()
    failing.run = lambda argv, **_: result(argv, "Docker Desktop", returncode=1)  # a failed query proves nothing
    assert default_data_location(failing, "linux")[0] == "bind"


def test_a_daemon_that_does_not_answer_is_said_in_the_reason_and_the_location_cannot_be_changed_later():
    silent = DockerStub()
    silent.run = lambda argv, **_: result(argv, returncode=1)
    location, reason = default_data_location(silent, "linux")
    assert location == "bind" and "did not answer" in reason and "--data-location volume" in reason
    answering = DockerStub()
    assert default_data_location(answering, "linux") == ("bind", "Linux host")


def test_native_windows_is_refused_with_a_clear_message(tmp_path, monkeypatch):
    import types
    from quadringent.installer.cli import _build_parser
    from quadringent.local import cli as local_cli

    monkeypatch.setattr(local_cli, "os", types.SimpleNamespace(**{k: v for k, v in vars(os).items() if k != "getuid"}))
    cli = Cli(tmp_path)
    args = _build_parser().parse_args(["local", "check", "--root", str(cli.root), "--name", "demo"])
    assert local_cli.run_local_command(args, cli.runner, cli.out, stderr=cli.err) == 2
    assert "Native Windows is not supported" in cli.err.getvalue() and "WSL2" in cli.err.getvalue()
    assert cli.runner.invocations == []


def test_init_chooses_the_volume_on_docker_desktop_and_bind_on_a_plain_linux_host(tmp_path):
    desktop = Cli(tmp_path / "a")
    desktop.runner.operating_system = "Docker Desktop"
    assert desktop.init() == 0
    assert desktop.config().data_location == "volume" and desktop.config().volume_name == VOLUME
    assert "Docker volume " + VOLUME in desktop.out.getvalue()
    mac = Cli(tmp_path / "b", platform="darwin")
    assert mac.init() == 0 and mac.config().data_location == "volume"
    linux = Cli(tmp_path / "c")
    assert linux.init() == 0 and linux.config().data_location == "bind"
    assert json.loads(linux.paths.config.read_text())["data_location"] == "bind"


def test_the_option_overrides_the_default_in_both_directions(tmp_path):
    forced_bind = Cli(tmp_path / "a", platform="darwin")
    assert forced_bind.init("--data-location", "bind") == 0
    assert forced_bind.config().data_location == "bind" and forced_bind.paths.marker.is_file()
    forced_volume = Cli(tmp_path / "b")
    assert forced_volume.init("--data-location", "volume") == 0
    assert forced_volume.config().data_location == "volume"
    with pytest.raises(SystemExit):  # argparse: only bind or volume
        Cli(tmp_path / "c").init("--data-location", "nfs")


def test_volume_init_creates_no_host_data_directory_and_no_docker_volume_yet(tmp_path):
    cli = volume_cli(tmp_path)
    assert not cli.paths.data.exists() and not cli.paths.marker.exists()
    assert volume_calls(cli) == [] and cli.runner.runs() == []
    cfg.check_layout(cli.paths, data=False)
    assert oct(cli.paths.config.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(cfg.ConfigError):  # the default check still wants a data directory
        cfg.check_layout(cli.paths)


def test_an_unknown_data_location_in_the_configuration_is_refused(tmp_path):
    cli = volume_cli(tmp_path)
    document = json.loads(cli.paths.config.read_text())
    document["data_location"] = "nfs"
    with pytest.raises(cfg.ConfigError, match="bind or volume"):
        cfg.from_dict(document)
    del document["data_location"]  # an older file means bind
    assert cfg.from_dict(document).data_location == "bind"


# -- start ---------------------------------------------------------------------------------------------------


def test_start_creates_the_labelled_volume_then_mounts_it_in_every_data_container(tmp_path):
    cli = started(tmp_path)
    assert cli.runner.roles() == list(VOLUME_ROLES)
    assert cli.runner.volumes == {VOLUME: {LABEL: "demo", ID_LABEL: cli.config().label}}
    create = volume_calls(cli, "create")
    assert create == [("docker", "volume", "create", "--label", "io.quadringent.local=demo",
                       "--label", f"{ID_LABEL}={cli.config().label}", VOLUME)]
    order = [argv[:3] if argv[:2] == ("docker", "volume") else ("run", role_of(argv)) for argv, _, _ in cli.runner.invocations
             if argv[:2] == ("docker", "volume") or argv[:2] == ("docker", "run")]
    assert order.index(("docker", "volume", "create")) < order.index(("run", "init-data"))
    data_roles = ("init-data", "fs-probe", "admit", "copy", "capture", "loader")
    for argv in cli.runner.runs():
        name = role(argv)
        mounted = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
        assert mounted == ([f"{VOLUME}:/data"] if name in data_roles else []), name
        assert not any("target=/data" in item for item in argv), name
        for flag in HARDENING:
            assert flag in argv, (name, flag)


def test_no_host_data_path_is_used_in_volume_mode(tmp_path):
    cli = started(tmp_path)
    host_data = str(cli.paths.data)
    for argv, cwd, env in cli.runner.invocations:
        assert not any(host_data in item for item in argv), argv
    assert not cli.paths.data.exists()
    for role_name in VOLUME_ROLES:
        assert host_data not in cli.paths.env_file(role_name).read_text()
    assert "Docker volume " + VOLUME in cli.out.getvalue() and "CLEAR TEXT" in cli.out.getvalue()
    assert load_run_state(cli.paths)["stages"]["copy"]


def test_init_data_runs_as_root_with_two_capabilities_no_network_and_hands_the_volume_to_the_host_user(tmp_path):
    cli = started(tmp_path)
    argv = cli.runner.runs("init-data")[0]
    assert option(argv, "--user") == "0:0"
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "--cap-add"] == ["CHOWN", "DAC_OVERRIDE"]
    assert option(argv, "--cap-drop") == "ALL" and option(argv, "--network") == "none"
    assert argv[-7:] == ("init-data", "--label", cli.config().label, "--uid", "1000", "--gid", "1000")
    assert option(argv, "--entrypoint") == "/opt/venv/bin/python"
    env = cli.env_file("init-data")
    assert env["AS400_LOCAL_DATA_DIR"] == "/data" and env["QUADRINGENT_STORAGE_BACKEND"] == "local"
    for other in cli.runner.runs():
        if role(other) != "init-data":
            assert option(other, "--user") == "1000:1000" and "--cap-add" not in other


def test_secrets_stay_read_only_host_file_mounts_in_volume_mode(tmp_path):
    cli = started(tmp_path)
    capture = [argv[i + 1] for argv in [cli.runner.runs("capture")[0]] for i, item in enumerate(argv) if item == "--mount"]
    assert any(f"source={cli.paths.ibmi_password.resolve()}" in spec and spec.endswith(",readonly") for spec in capture)
    loader = [argv[i + 1] for argv in [cli.runner.runs("loader")[0]] for i, item in enumerate(argv) if item == "--mount"]
    assert any(f"source={cli.paths.snowflake_key.resolve()}" in spec and spec.endswith(",readonly") for spec in loader)


def test_a_second_start_reuses_the_volume_and_does_not_recreate_it(tmp_path):
    cli = started(tmp_path)
    assert cli.start() == 0
    assert len(volume_calls(cli, "create")) == 1
    assert cli.runner.roles().count("init-data") == 2  # idempotent helper, run again
    assert cli.runner.roles().count("copy") == 1  # stages are still skipped


def test_start_refuses_an_existing_volume_that_this_tool_did_not_label(tmp_path):
    cli = volume_cli(tmp_path)
    cli.runner.volumes[VOLUME] = {}
    assert cli.start() == 1
    assert "not labelled io.quadringent.local=demo" in cli.err.getvalue()
    assert cli.runner.runs() == [] and volume_calls(cli, "create") == [] and volume_calls(cli, "rm") == []
    cli.runner.volumes[VOLUME] = {LABEL: "other"}
    assert cli.start() == 1 and cli.runner.runs() == []


def test_a_failing_init_data_stops_start_before_any_container_touches_the_volume(tmp_path):
    cli = volume_cli(tmp_path)
    cli.runner.handlers["init-data"] = lambda argv: result(argv, returncode=1, stderr="quadringent_local_error=storage_invalid: x")
    assert cli.start() == 1 and "preparing the data volume failed" in cli.err.getvalue()
    assert cli.runner.roles() == ["init-data"]


def test_the_gate_failure_message_names_the_volume_and_says_the_gate_decides(tmp_path):
    cli = volume_cli(tmp_path)
    cli.runner.handlers["fs-probe"] = lambda argv: result(
        argv, RESULT + json.dumps({"ok": False, "checks": {"flock": {"ok": False, "detail": "x"}}}) + "\n", returncode=1)
    assert cli.start() == 1
    message = cli.err.getvalue()
    assert VOLUME in message and "failed: flock" in message and "gate decides" in message
    assert cli.runner.roles() == ["init-data", "fs-probe"]


def test_changing_the_data_location_under_a_recorded_run_is_refused(tmp_path):
    cli = started(tmp_path)
    document = json.loads(cli.paths.config.read_text())
    document["data_location"] = "bind"
    cli.paths.config.write_text(json.dumps(document))
    cfg.prepare_data_dir(cli.paths, cli.config().label)  # a host folder that looks valid: the recorded run still refuses
    assert cli.start() == 1 and "configuration changed" in cli.err.getvalue()


def test_check_in_volume_mode_needs_no_host_data_directory(tmp_path):
    cli = volume_cli(tmp_path)
    assert cli.invoke("check", "--json") in (0, 1, 3)
    report = json.loads(cli.out.getvalue())
    instance = next(item for item in report["results"] if item["code"] == "local_config.instance")
    assert instance["status"] == "passed" and VOLUME in instance["summary"]


# -- status --------------------------------------------------------------------------------------------------


def test_status_reads_the_snapshot_through_a_read_only_helper_never_from_a_host_path(tmp_path):
    cli = started(tmp_path)
    cli.runner.console = {"generated_at": "2026-10-13T10:00:00Z", "run": {"state": "RUNNING", "stopped_because": None},
                          "position": {"checkpoint": "RCV:4300"}, "lag": {"current": {"known": True, "value": 12}},
                          "counters": {"events_published": {"known": True, "value": 40}, "errors": {"known": True, "value": 0},
                                       "polls": {"known": True, "value": 9}}}
    cli.runner.evidence_rows = 3
    before = len(cli.runner.runs())
    assert cli.invoke("status", "--json") == 0
    document = json.loads(cli.out.getvalue())
    assert document["data_location"] == "volume" and document["data_directory"] is None  # a path, or null
    assert document["data_volume"] == VOLUME
    assert document["capture"]["lag_sequences"] == 12 and document["capture"]["events_published"] == 40
    assert document["initial_copy_evidence_rows"] == 3 and document["initial_copy_rows"] == 3
    helper = cli.runner.runs()[before:]
    assert [role(argv) for argv in helper] == ["read-status"]
    argv = helper[0]
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "-v"] == [f"{VOLUME}:/data:ro"]
    assert option(argv, "--network") == "none" and option(argv, "--user") == "1000:1000"
    assert option(argv, "--pull") == "never" and option(argv, "--name").startswith("qdt-local.demo.read-status.")
    assert "--evidence-key" in argv and option(argv, "--evidence-key").endswith(".json")
    assert not any(str(cli.paths.data) in item for item in argv)
    assert cli.invoke("status") == 0 and "Data volume: docker volume " + VOLUME in cli.out.getvalue()
    assert "lag 12" in cli.out.getvalue()


def test_status_never_pulls_an_image_and_names_each_helper_run_differently(tmp_path):
    cli = started(tmp_path)
    cli.runner.missing_images.add(cli.config().capture_image)
    assert cli.invoke("status", "--json") == 1
    assert "not present locally" in cli.out.getvalue() and "never pulls" in cli.out.getvalue()
    assert not any(argv[:2] == ("docker", "pull") for argv, _, _ in cli.runner.invocations)
    assert "read-status" not in cli.runner.roles()
    cli.runner.missing_images.clear()
    cli.out.truncate(0), cli.out.seek(0)
    assert cli.invoke("status") == 0 and cli.invoke("status") == 0
    names = [option(argv, "--name") for argv in cli.runner.runs("read-status")]
    assert len(names) == 2 and names[0] != names[1]


def test_status_does_not_read_a_volume_that_only_has_this_instances_name(tmp_path):
    cli = started(tmp_path)
    cli.runner.volumes[VOLUME] = {LABEL: "demo", ID_LABEL: "qdt-ffffffff"}
    assert cli.invoke("status", "--json") == 1
    assert "another instance" in cli.out.getvalue() and "read-status" not in cli.runner.roles()


def test_status_does_not_create_a_missing_volume(tmp_path):
    cli = volume_cli(tmp_path)
    assert cli.invoke("status", "--json") == 0
    assert json.loads(cli.out.getvalue())["capture"] is None
    assert cli.runner.runs() == [] and volume_calls(cli, "create") == []


def test_status_reports_an_unreadable_volume_as_a_problem(tmp_path):
    cli = started(tmp_path)
    cli.runner.handlers["read-status"] = lambda argv: result(argv, returncode=1, stderr="quadringent_local_error=config_invalid: x")
    assert cli.invoke("status", "--json") == 1
    document = json.loads(cli.out.getvalue())
    assert "data volume could not be read" in document["problem"] and document["capture"] is None


# -- reset ---------------------------------------------------------------------------------------------------


def test_reset_needs_yes_then_removes_the_labelled_volume_and_leaves_the_next_start_to_create_it(tmp_path):
    cli = started(tmp_path)
    decoy = cli.paths.data
    decoy.mkdir()
    (decoy / "keep.txt").write_text("a host folder is never touched in volume mode")
    assert cli.invoke("reset") == 2 and volume_calls(cli, "rm") == []
    before = len(cli.runner.runs())
    assert cli.invoke("reset", "--yes") == 0, cli.out.getvalue()
    assert volume_calls(cli, "rm") == [("docker", "volume", "rm", VOLUME)]
    assert (decoy / "keep.txt").exists()
    assert cli.runner.runs()[before:] == []  # no container, so no image is needed (and none is pulled)
    assert not any(argv[:2] == ("docker", "pull") for argv, _, _ in cli.runner.invocations)
    assert cli.runner.volumes == {}
    assert not cli.paths.run_json.exists() and cli.runner.states == {}
    assert "Data volume and run state deleted" in cli.out.getvalue()
    assert "Snowflake tables are not touched" in cli.out.getvalue()
    assert cli.start() == 0 and load_run_state(cli.paths)["stages"]["copy"]
    assert list(cli.runner.volumes) == [VOLUME]


@pytest.mark.parametrize("make_labels", [
    lambda label: {},
    lambda label: {LABEL: "other", ID_LABEL: label},
    lambda label: {"com.example.other": "demo"},
    lambda label: {LABEL: "demo"},  # same name, but no instance id
    lambda label: {LABEL: "demo", ID_LABEL: "qdt-ffffffff"},  # same name, another instance
])
def test_reset_refuses_a_volume_without_this_instances_labels(tmp_path, make_labels):
    cli = volume_cli(tmp_path)
    labels = make_labels(cli.config().label)
    cli.runner.volumes[VOLUME] = dict(labels)
    assert cli.invoke("reset", "--yes") == 1
    assert "Docker volume " + VOLUME in cli.out.getvalue() and "refusing to use or delete" in cli.out.getvalue()
    assert volume_calls(cli, "rm") == [] and cli.runner.volumes == {VOLUME: labels}
    assert cli.runner.runs() == []


def test_two_instances_with_the_same_name_under_different_roots_never_share_a_volume(tmp_path):
    first = started(tmp_path / "a")
    second = volume_cli(tmp_path / "b")
    assert first.config().label != second.config().label
    second.runner.volumes = first.runner.volumes  # one Docker daemon, two config roots, one volume name
    assert second.invoke("reset", "--yes") == 1
    assert "another instance that has the same name" in second.out.getvalue()
    assert volume_calls(second, "rm") == [] and VOLUME in first.runner.volumes
    assert second.start() == 1 and "another instance that has the same name" in second.err.getvalue()
    assert second.runner.runs() == []


def test_a_failing_volume_inspect_is_not_read_as_an_absent_volume(tmp_path):
    cli = started(tmp_path)
    cli.runner.inspect_error = "Error response from daemon: context deadline exceeded"
    before, runs = len(cli.runner.invocations), len(cli.runner.runs())
    assert cli.invoke("reset", "--yes") == 1
    assert "cannot inspect the Docker volume" in cli.out.getvalue() and "deadline exceeded" in cli.out.getvalue()
    assert cli.paths.run_json.exists() and VOLUME in cli.runner.volumes  # nothing was forgotten or removed
    assert cli.start() == 1 and "cannot inspect the Docker volume" in cli.err.getvalue()
    cli.out.truncate(0), cli.out.seek(0)
    assert cli.invoke("status", "--json") == 1
    assert "cannot inspect the Docker volume" in json.loads(cli.out.getvalue())["problem"]
    new = cli.runner.invocations[before:]
    assert not [argv for argv, _, _ in new if argv[:3] in (("docker", "volume", "create"), ("docker", "volume", "rm"))]
    assert len(cli.runner.runs()) == runs


def test_reset_with_no_volume_yet_removes_only_the_run_state(tmp_path):
    cli = volume_cli(tmp_path)
    assert cli.invoke("reset", "--yes") == 0
    assert volume_calls(cli, "rm") == [] and cli.runner.volumes == {} and cli.runner.runs() == []


def test_reset_reports_a_volume_that_docker_cannot_remove(tmp_path):
    cli = started(tmp_path)
    run = cli.runner.run

    def failing(argv, **kwargs):
        if tuple(argv[:3]) == ("docker", "volume", "rm"):
            return result(argv, returncode=1, stderr="Error: volume is in use")
        return run(argv, **kwargs)

    cli.runner.run = failing
    assert cli.invoke("reset", "--yes") == 1 and "volume is in use" in cli.out.getvalue()
    assert cli.paths.run_json.exists()  # nothing was forgotten


# -- bind mode is unchanged ------------------------------------------------------------------------------------


def test_bind_mode_keeps_its_bind_mounts_helpers_and_never_touches_docker_volumes(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init("--data-location", "bind") == 0 and cli.start() == 0
    assert "init-data" not in cli.runner.roles() and volume_calls(cli) == []
    assert not any(argv[:2] == ("docker", "info") for argv, _, _ in cli.runner.invocations)
    for argv in cli.runner.runs():
        assert "-v" not in argv and "--cap-add" not in argv and "--network" not in argv
        if role(argv) in ("fs-probe", "admit", "copy", "capture", "loader"):
            assert f"type=bind,source={cli.paths.data.resolve()},target=/data" in argv
    assert cli.invoke("status", "--json") == 0
    document = json.loads(cli.out.getvalue())
    assert document["data_location"] == "bind" and document["data_directory"] == str(cli.paths.data)
    assert "read-status" not in cli.runner.roles()
    assert cli.invoke("reset", "--yes") == 0 and cli.paths.marker.exists() and volume_calls(cli) == []


def test_the_bind_fingerprint_is_the_one_runs_recorded_before_the_setting_existed(tmp_path):
    from quadringent.local.orchestrator import identity_fingerprint

    bind, volume = Cli(tmp_path / "a"), Cli(tmp_path / "b")
    bind.init("--data-location", "bind")
    volume.init("--data-location", "volume")
    config = bind.config()
    import dataclasses
    assert identity_fingerprint(config) != identity_fingerprint(dataclasses.replace(config, data_location="volume"))


# -- plan units ----------------------------------------------------------------------------------------------


def test_volume_mounts_are_named_volumes_and_refuse_anything_that_could_be_a_path():
    assert plan.VolumeMount("qdt-local-demo-data", "/data").argv() == ("-v", "qdt-local-demo-data:/data")
    assert plan.VolumeMount("qdt-local-demo-data", "/data", read_only=True).argv() == ("-v", "qdt-local-demo-data:/data:ro")
    for name in ("/etc", "./data", "a:b", "-x", "a b", ""):
        with pytest.raises(ValueError):
            plan.VolumeMount(name, "/data").argv()
    with pytest.raises(ValueError):
        plan.VolumeMount("vol", "/data:rw").argv()


def test_every_data_spec_of_a_volume_instance_uses_the_volume_mount(tmp_path):
    cli = volume_cli(tmp_path)
    config, paths = cli.config(), cli.paths
    specs = [plan.init_data_spec(config, paths, uid=1, gid=2), plan.read_status_spec(config, paths),
             plan.fs_probe_spec(config, paths),
             plan.admit_spec(config, paths, boundary_json="{}", journal=("L", "J")),
             plan.capture_spec(config, paths, time_zone="Europe/Paris", capture_env={}),
             plan.loader_spec(config, paths, table_set_json="[]")]
    for spec in specs:
        assert any(isinstance(m, plan.VolumeMount) and m.target == "/data" for m in spec.mounts), spec.role
        assert not any(isinstance(m, plan.Mount) and m.target == "/data" for m in spec.mounts), spec.role
    assert plan.container_name("demo", "init-data") in {plan.container_name("demo", r) for r in plan.ROLES_ONE_SHOT}


# -- the in-image helpers --------------------------------------------------------------------------------------


def runtime():
    from quadringent_control_plane.v2.executor import local_runtime
    return local_runtime


def run_init(rt, data, label="qdt-0a1b2c3d", uid=None, gid=None):
    args = rt.build_parser().parse_args(["init-data", "--data-dir", str(data), "--label", label,
                                         "--uid", str(os.getuid() if uid is None else uid),
                                         "--gid", str(os.getgid() if gid is None else gid)])
    return rt.command_init_data(args)


def test_init_data_creates_the_marker_and_subdirectories_and_is_idempotent(tmp_path):
    rt = runtime()
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    document, code = run_init(rt, data)
    assert code == 0 and document == {"ok": True, "created": True, "label": "qdt-0a1b2c3d"}
    marker = data / ".quadringent-local"
    assert json.loads(marker.read_text()) == {"format_version": 1, "label": "qdt-0a1b2c3d"}
    assert marker.read_text() == cfg.marker_document("qdt-0a1b2c3d").decode()  # same bytes as the host writes
    assert oct(marker.stat().st_mode & 0o777) == "0o600" and oct(data.stat().st_mode & 0o777) == "0o700"
    for child in ("console", "scratch"):
        assert (data / child).is_dir() and oct((data / child).stat().st_mode & 0o777) == "0o700"
    (data / "objects").mkdir()
    (data / "objects" / "kept.json").write_text("{}")
    again, _ = run_init(rt, data)
    assert again["created"] is False and (data / "objects" / "kept.json").exists()
    from quadringent.storage_backend import require_local_marker
    require_local_marker(str(data), "qdt-0a1b2c3d")


def test_init_data_refuses_a_marker_of_another_store_a_link_and_a_bad_label(tmp_path):
    rt = runtime()
    data = tmp_path / "data"
    data.mkdir()
    run_init(rt, data)
    with pytest.raises(rt.LocalRuntimeError, match="another store"):
        run_init(rt, data, label="qdt-ffffffff")
    with pytest.raises(rt.LocalRuntimeError):
        run_init(rt, data, label="NOT A LABEL")
    link = tmp_path / "link"
    link.symlink_to(data)
    with pytest.raises(rt.LocalRuntimeError, match="not a link"):
        run_init(rt, link)
    with pytest.raises(rt.LocalRuntimeError, match="does not exist"):
        run_init(rt, tmp_path / "absent")
    sub = tmp_path / "other"
    sub.mkdir()
    (sub / "console").symlink_to(data)
    with pytest.raises(rt.LocalRuntimeError, match="real directory"):
        run_init(rt, sub)


def test_init_data_does_not_follow_a_planted_marker_link(tmp_path):
    rt = runtime()
    data = tmp_path / "data"
    data.mkdir()
    victim = tmp_path / "victim"
    victim.write_text(json.dumps({"format_version": 1, "label": "qdt-0a1b2c3d"}))
    (data / ".quadringent-local").symlink_to(victim)
    with pytest.raises(rt.LocalRuntimeError):
        run_init(rt, data)
    assert json.loads(victim.read_text()) == {"format_version": 1, "label": "qdt-0a1b2c3d"}  # untouched
    assert (data / ".quadringent-local").is_symlink()


def test_init_data_prints_one_result_line_through_main(tmp_path, capsys):
    rt = runtime()
    data = tmp_path / "data"
    data.mkdir()
    assert rt.main(["init-data", "--data-dir", str(data), "--label", "qdt-0a1b2c3d",
                    "--uid", str(os.getuid()), "--gid", str(os.getgid())]) == 0
    out = capsys.readouterr().out
    assert out.startswith(RESULT) and out.count("\n") == 1


def test_read_status_returns_the_console_snapshot_and_the_copy_evidence_rows(tmp_path, monkeypatch):
    rt = runtime()
    data = tmp_path / "data"
    data.mkdir()
    run_init(rt, data)
    (data / "console" / "capture.json").write_text(json.dumps({"run": {"state": "RUNNING"}}))
    evidence = data / "objects" / "qdt-0a1b2c3d" / "raw" / "t1" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "run.json").write_text(json.dumps({"rows_copied": 7}))
    monkeypatch.setenv("AS400_RAW_BUCKET", "qdt-0a1b2c3d")
    args = rt.build_parser().parse_args(["read-status", "--data-dir", str(data),
                                         "--evidence-key", "raw/t1/evidence/run.json"])
    assert rt.command_read_status(args) == ({"console": {"run": {"state": "RUNNING"}}, "evidence_rows": 7}, 0)
    args = rt.build_parser().parse_args(["read-status", "--data-dir", str(data)])
    assert rt.command_read_status(args)[0] == {"console": {"run": {"state": "RUNNING"}}, "evidence_rows": None}


def test_read_status_tolerates_absent_oversized_linked_and_malformed_files_and_refuses_odd_keys(tmp_path, monkeypatch):
    rt = runtime()
    data = tmp_path / "data"
    (data / "console").mkdir(parents=True)
    monkeypatch.setenv("AS400_RAW_BUCKET", "qdt-0a1b2c3d")
    parse = rt.build_parser().parse_args
    assert rt.command_read_status(parse(["read-status", "--data-dir", str(data)]))[0]["console"] is None
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (data / "console" / "capture.json").symlink_to(target)
    assert rt.command_read_status(parse(["read-status", "--data-dir", str(data)]))[0]["console"] is None
    (data / "console" / "capture.json").unlink()
    (data / "console" / "capture.json").write_text("not json")
    assert rt.command_read_status(parse(["read-status", "--data-dir", str(data)]))[0]["console"] is None
    (data / "console" / "capture.json").write_text(" " * (rt.MAX_CONSOLE_BYTES + 1))
    assert rt.command_read_status(parse(["read-status", "--data-dir", str(data)]))[0]["console"] is None
    (data / "console" / "capture.json").write_text("[1]")
    assert rt.command_read_status(parse(["read-status", "--data-dir", str(data)]))[0]["console"] is None
    for key in ("../escape.json", "raw/../x", "raw//x"):
        with pytest.raises(rt.LocalRuntimeError):
            rt.command_read_status(parse(["read-status", "--data-dir", str(data), "--evidence-key", key]))
