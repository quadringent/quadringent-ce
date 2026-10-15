"""Docker plans of ``quadringent local``: hardening flags, secret handling, plaintext pairing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_cli_support import IBMI_HOST, PASSWORD, Cli, option
from quadringent.local import dockerplan as plan

HARDENING = ("--init", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges")
ALL_ROLES = ("fs-probe", "probe", "discover", "boundary", "admit", "copy", "capture", "loader")


def started(tmp_path, *init_args, name="demo"):
    cli = Cli(tmp_path, name=name)
    assert cli.init(*init_args) == 0
    assert cli.start() == 0, cli.err.getvalue()
    return cli


def mounts(argv):
    return [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]


def test_every_container_carries_the_hardening_flags_labels_and_fixed_names(tmp_path):
    cli = started(tmp_path)
    runs = cli.runner.runs()
    assert [plan_role for plan_role in cli.runner.roles()] == list(ALL_ROLES)
    for argv in runs:
        role = next(i.split("=", 1)[1] for i in argv if i.startswith("io.quadringent.local.role="))
        for flag in HARDENING:
            assert flag in argv, (role, flag)
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        for limit in ("--pids-limit", "--memory", "--cpus"):
            assert option(argv, limit)
        assert option(argv, "--user") == "1000:1000"
        assert option(argv, "--tmpfs") == plan.TMPFS and "HOME=/tmp" in argv
        assert "io.quadringent.local=demo" in argv
        assert option(argv, "--name") == f"qdt-local.demo.{role}"
        assert "--privileged" not in argv and "--network" not in argv and "-p" not in argv
        assert option(argv, "--env-file") == str(cli.paths.env_file(role))
        if role in plan.ROLES_SERVICE:
            assert "-d" in argv and option(argv, "--restart") == "on-failure:3" and "--rm" not in argv
        else:
            assert "--rm" in argv and "--restart" not in argv


def test_secrets_are_read_only_file_mounts_and_never_appear_in_arguments_or_env_files(tmp_path):
    cli = started(tmp_path)
    key_text = cli.paths.snowflake_key.read_text()
    key_body = [line for line in key_text.splitlines() if "KEY" not in line][0]
    password_target, key_target = plan.PASSWORD_TARGET, plan.KEY_TARGET
    seen_password_mount = seen_key_mount = False
    for argv in cli.runner.runs():
        joined = " ".join(argv)
        assert PASSWORD not in joined and key_body not in joined
        for spec in mounts(argv):
            if f"target={password_target}" in spec:
                seen_password_mount = True
                assert spec.endswith(",readonly") and f"source={cli.paths.ibmi_password.resolve()}" in spec
            if f"target={key_target}" in spec:
                seen_key_mount = True
                assert spec.endswith(",readonly") and f"source={cli.paths.snowflake_key.resolve()}" in spec
            if "target=/data" in spec:
                assert not spec.endswith(",readonly")
    assert seen_password_mount and seen_key_mount
    for role in ALL_ROLES:
        text = cli.paths.env_file(role).read_text()
        assert PASSWORD not in text and key_body not in text and "BEGIN" not in text
        env = cli.env_file(role)
        assert "ISERIES_PASSWORD" not in env and "SNOWFLAKE_PRIVATE_KEY_PEM" not in env
        assert oct(cli.paths.env_file(role).stat().st_mode & 0o777) == "0o600"
    # nothing else on disk outside secrets/ holds either secret
    for path in cli.paths.directory.rglob("*"):
        if path.is_file() and cli.paths.secrets not in path.parents:
            data = path.read_bytes()
            assert PASSWORD.encode() not in data and key_body.encode() not in data, path


def test_the_loader_and_verifier_get_only_the_key_and_the_capture_roles_only_the_password(tmp_path):
    cli = started(tmp_path)
    for argv in cli.runner.runs():
        specs = " ".join(mounts(argv))
        role = next(i.split("=", 1)[1] for i in argv if i.startswith("io.quadringent.local.role="))
        wants_password = role in ("probe", "discover", "boundary", "copy", "capture")
        wants_key = role in ("loader", "verify")
        assert (plan.PASSWORD_TARGET in specs) == wants_password, role
        assert (plan.KEY_TARGET in specs) == wants_key, role
        assert ("target=/data" in specs) == (role in ("fs-probe", "admit", "copy", "capture", "loader")), role


def test_images_and_entrypoints_follow_the_image_that_carries_each_job(tmp_path):
    cli = started(tmp_path, "--capture-image", "registry.example.test/cap:1", "--loader-image", "registry.example.test/cp@sha256:" + "b" * 64)
    for argv in cli.runner.runs():
        role = next(i.split("=", 1)[1] for i in argv if i.startswith("io.quadringent.local.role="))
        entry_at = argv.index("--entrypoint")
        if role == "loader":
            assert argv[entry_at + 1] == "python" and argv[entry_at + 2].endswith("@sha256:" + "b" * 64)
        else:
            assert argv[entry_at + 1] == "/opt/venv/bin/python" and argv[entry_at + 2] == "registry.example.test/cap:1"
        assert argv[entry_at + 3:entry_at + 6] == ("-P", "-m", plan.RUNTIME_MODULE)
    capture = cli.runner.runs("capture")[0]
    assert capture[-4:] == ("exec-capture", "--", "--console-snapshot-path", plan.CONSOLE_TARGET)
    probe = cli.runner.runs("probe")[0]
    assert "--script" in probe and option(probe, "--script") == "/app/quadringent_source_probe_job.py"
    assert cli.runner.runs("loader")[0][-1] == "exec-loader"


def test_tls_is_the_default_and_the_environment_has_no_plaintext_switch(tmp_path):
    cli = started(tmp_path)
    for role in ("probe", "discover", "boundary", "copy", "capture"):
        env = cli.env_file(role)
        assert env["AS400_TLS"] == "true" and "AS400_ALLOW_PLAINTEXT" not in env
        assert env["ISERIES_HOST"] == IBMI_HOST and env["ISERIES_USER"] == "SVCUSER"


def test_plaintext_sets_the_engine_pair_warns_loudly_and_is_recorded_in_the_configuration(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init("--allow-plaintext-ibmi", "--source-time-zone", "Europe/Paris") == 0
    assert "unencrypted" in cli.err.getvalue()
    config = json.loads(cli.paths.config.read_text())
    assert config["ibmi"]["tls"] is False and config["ibmi"]["allow_plaintext"] is True
    assert cli.start() == 0
    assert "unencrypted" in cli.err.getvalue()
    assert "probe" not in cli.runner.roles()  # the probe job requires TLS
    for role in ("discover", "boundary", "copy", "capture"):
        env = cli.env_file(role)
        assert (env["AS400_TLS"], env["AS400_ALLOW_PLAINTEXT"]) == ("false", "true"), role
        if role != "discover":
            assert env["AS400_SOURCE_TIME_ZONE"] == "Europe/Paris"


def test_explicit_ports_and_the_private_ca_reach_the_jobs(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nQ0E=\n-----END CERTIFICATE-----\n")
    cli = started(tmp_path, "--ibmi-ca-file", str(ca), "--database-port", "9500", "--signon-port", "9501",
                  "--command-port", "9502")
    env = cli.env_file("capture")
    assert (env["AS400_DATABASE_PORT"], env["AS400_SIGNON_PORT"], env["AS400_COMMAND_PORT"]) == ("9500", "9501", "9502")
    assert env["AS400_TLS_CA_FILE"] == plan.CA_TARGET
    assert any(f"target={plan.CA_TARGET}" in spec and spec.endswith(",readonly")
               for spec in mounts(cli.runner.runs("probe")[0]))
    assert option(cli.runner.runs("probe")[0], "--port") == "9501"
    assert cli.paths.ca_file.read_text().startswith("-----BEGIN CERTIFICATE")


def test_the_checkout_option_bind_mounts_code_read_only_over_the_images(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    cli = started(tmp_path, "--from-checkout", str(checkout))
    assert "developer-only" in cli.err.getvalue()
    capture = " ".join(mounts(cli.runner.runs("capture")[0]))
    loader = " ".join(mounts(cli.runner.runs("loader")[0]))
    assert f"source={checkout}/src/quadringent,target=/app/quadringent,readonly" in capture
    assert f"source={checkout}/src/quadringent,target=/app/src/quadringent,readonly" in loader
    assert "target=/app/quadringent_destination_loader.py,readonly" in loader
    assert "/app/quadringent_destination_loader.py" not in capture
    plain = started(tmp_path / "other")
    assert not any("/app/" in spec for spec in mounts(plain.runner.runs("capture")[0]))


def test_a_comma_in_a_mount_path_is_refused_not_split(tmp_path):
    with pytest.raises(ValueError):
        plan.Mount(Path("/a,b"), "/x").argv()


def test_env_files_refuse_values_that_would_break_the_file_format():
    assert plan.env_text({"A": "1", "B": "x=y"}) == "A=1\nB=x=y\n"
    with pytest.raises(ValueError):
        plan.env_text({"A": "line\nbreak"})


def test_stop_stops_and_removes_only_this_instances_containers(tmp_path):
    cli = started(tmp_path)
    owned_ids = {item["Id"] for item in cli.runner.containers.values()}
    foreign_name = "qdt-local.other.capture"
    cli.runner.add_container(foreign_name, {plan.LABEL: "other"})
    assert cli.invoke("stop") == 0
    calls = [argv for argv, _, _ in cli.runner.invocations if argv[:2] in (("docker", "stop"), ("docker", "rm")) and "-f" not in argv]
    assert {argv[-1] for argv in calls} == owned_ids
    assert cli.runner.states == {foreign_name: "running"}
    assert cli.invoke("stop") == 0 and "Stopped 0" in cli.out.getvalue()


def test_container_names_cannot_collide_across_instances():
    names = {plan.container_name(instance, role) for instance, role in (("ab", "fs-probe"), ("ab-fs", "probe"))}
    assert len(names) == 2
    assert plan.container_name("ab-fs", "probe") == "qdt-local.ab-fs.probe"


def test_a_secret_that_became_a_link_is_never_mounted(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    target = tmp_path / "elsewhere"
    target.write_text("x")
    cli.paths.snowflake_key.unlink()
    cli.paths.snowflake_key.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        plan.key_mount(cli.paths)
    with pytest.raises(ValueError, match="symbolic link"):
        plan.verify_spec(cli.config(), cli.paths)
