"""``quadringent local init``: private files, refused links and modes, no secret in configuration."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from local_cli_support import IBMI_HOST, PASSWORD, Cli
from quadringent.installer.cli import _build_parser, run
from quadringent.local import config as cfg
from quadringent.local.secrets import PrivateFileError, check_private_file, read_password


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_init_creates_private_layout_and_stores_no_secret_in_configuration(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init() == 0
    paths = cli.paths
    for directory in (paths.directory, paths.secrets, paths.data, paths.state):
        assert mode(directory) == 0o700, directory
    for file in (paths.config, paths.ibmi_password, paths.snowflake_key, paths.setup_script, paths.marker):
        assert mode(file) == 0o600, file
    assert paths.ibmi_password.read_text() == PASSWORD
    assert "BEGIN PRIVATE KEY" in paths.snowflake_key.read_text()
    config_text = paths.config.read_text()
    assert PASSWORD not in config_text and "PRIVATE KEY" not in config_text and "password" not in config_text.lower()
    # the setup script holds the public key only
    script = paths.setup_script.read_text()
    assert "PRIVATE KEY" not in script and "RSA_PUBLIC_KEY" in script
    marker = json.loads(paths.marker.read_text())
    assert marker == {"format_version": 1, "label": cli.config().label}
    assert cfg.LABEL.fullmatch(marker["label"]) and marker["label"].startswith("qdt-")


def test_snowflake_script_prints_the_stored_public_key_script(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    assert cli.invoke("snowflake-script") == 0
    assert cli.out.getvalue() == cli.paths.setup_script.read_text()
    assert cli.config().snowflake.role in cli.out.getvalue()


def test_password_comes_from_a_file_stdin_or_a_hidden_prompt_never_an_argument(tmp_path, monkeypatch):
    file = tmp_path / "pw"
    file.write_text("from-file\r\n")
    cli = Cli(tmp_path / "a")
    assert cli.init("--ibmi-password-file", str(file)) == 0
    assert cli.paths.ibmi_password.read_text() == "from-file"
    cli = Cli(tmp_path / "b")
    assert cli.init(password="from-stdin\n") == 0
    assert cli.paths.ibmi_password.read_text() == "from-stdin"

    class Terminal:
        def isatty(self):
            return True

    assert read_password(password_file=None, stdin=Terminal(), prompt=lambda text: "typed") == "typed"
    with pytest.raises(SystemExit):  # no value flag exists, and abbreviations are disabled
        _build_parser().parse_args(["local", "init", "--ibmi-password", "x", "--ibmi-host", "h", "--ibmi-user", "U",
                                    "--table", "A/B", "--snowflake-account", "a"])
    for bad in ("", "\n", "two\nlines\n"):
        with pytest.raises(PrivateFileError):
            read_password(password_file=None, stdin=__import__("io").StringIO(bad))


def test_init_refuses_an_existing_instance_and_cleans_up_after_a_failure(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init() == 0
    assert cli.init() == 2 and "already exists" in cli.err.getvalue()
    cli = Cli(tmp_path, name="broken")
    assert cli.init("--ibmi-ca-file", str(tmp_path / "missing.pem")) == 2
    assert not cli.paths.directory.exists()  # nothing half-created is left behind


def test_unsafe_modes_and_links_are_refused(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    cli.paths.secrets.chmod(0o755)
    with pytest.raises(cfg.ConfigError, match="too open"):
        cfg.check_layout(cli.paths)
    assert cli.start() == 1 and "too open" in cli.err.getvalue()
    assert not cli.runner.runs()  # no container started with unsafe secrets
    cli.paths.secrets.chmod(0o700)
    cli.paths.ibmi_password.chmod(0o640)
    with pytest.raises(cfg.ConfigError, match="0600"):
        cfg.check_layout(cli.paths)
    cli.paths.ibmi_password.chmod(0o600)
    cfg.check_layout(cli.paths)


def test_symbolic_links_are_refused_for_secrets_config_and_the_instance_directory(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    real = cli.paths.snowflake_key
    moved = tmp_path / "elsewhere.p8"
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(cfg.ConfigError, match="symbolic link"):
        cfg.check_layout(cli.paths)
    with pytest.raises(PrivateFileError):
        check_private_file(real)
    real.unlink()
    moved.rename(real)

    other = Cli(tmp_path, name="linked")
    other.root.mkdir(exist_ok=True)
    other.paths.directory.symlink_to(cli.paths.directory)
    assert other.invoke("snowflake-script") == 2 and "symbolic link" in other.err.getvalue()


def test_a_data_directory_marker_for_another_label_is_refused(tmp_path):
    cli = Cli(tmp_path)
    cli.init()
    cli.paths.marker.write_text(json.dumps({"format_version": 1, "label": "qdt-ffffffff"}))
    with pytest.raises(cfg.ConfigError, match="does not match"):
        cfg.prepare_data_dir(cli.paths, cli.config().label)


@pytest.mark.parametrize("override, message", [
    ({"--allow-plaintext-ibmi": None}, "source-time-zone"),
    ({"--source-time-zone": "Mars/Olympus"}, "time zone"),
    ({"--snowflake-account": "bad account"}, "account"),
    ({"--capture-image": "UPPER case"}, "image"),
])
def test_invalid_settings_are_refused_before_anything_is_written(tmp_path, override, message):
    cli = Cli(tmp_path)
    options = {"--ibmi-host": IBMI_HOST, "--ibmi-user": "u", "--table": "L/T", "--snowflake-account": "org-acct"}
    options.update(override)
    argv = [part for key, value in options.items() for part in ((key,) if value is None else (key, value))]
    assert cli.invoke("init", *argv, stdin="pw\n") == 2
    assert message in cli.err.getvalue().lower()
    assert not cli.paths.directory.exists()


def test_the_configuration_round_trips_and_derives_the_warehouse_from_the_role(tmp_path):
    cli = Cli(tmp_path)
    cli.init("--snowflake-schema", "mirror1")
    config = cli.config()
    assert cfg.from_dict(config.to_dict()) == config
    assert config.warehouse == "QDT_WH_" + config.snowflake.role[len("QDT_ROLE_"):]
    assert (config.history_schema, config.mirror_schema) == ("MIRROR1", "MIRROR1")
    assert (config.table_schema, config.table_name) == ("QDCLIB", "ORDERS") and config.ibmi.user == "SVCUSER"


def test_local_is_registered_in_the_main_parser(capsys):
    with pytest.raises(SystemExit) as stop:
        run(["local", "--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    for command in ("init", "snowflake-script", "check", "start", "status", "stop", "reset"):
        assert command in text
    args = _build_parser().parse_args(["local", "status", "--json", "--logs"])
    assert (args.command, args.local_command, args.json, args.logs) == ("local", "status", True, True)


def test_the_host_package_imports_no_module_that_needs_fcntl_or_the_runtime():
    import subprocess

    code = (
        "import sys, quadringent.local.cli, quadringent.local.orchestrator, quadringent.local.check\n"
        "bad = [m for m in ('quadringent.checkpoint', 'quadringent.source_gate', 'quadringent.object_store',\n"
        "                   'quadringent_control_plane', 'boto3') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert done.returncode == 0, done.stderr


def test_the_default_images_follow_the_package_version_and_overrides_accept_digests(tmp_path):
    from quadringent.local.images import default_images, resolve_images

    assert default_images("1.2.3") == ("ghcr.io/quadringent/quadringent-ce:1.2.3-capture",
                                        "ghcr.io/quadringent/quadringent-ce:1.2.3-control-plane")
    digest = "registry.example.test:5000/team/capture@sha256:" + "a" * 64
    assert resolve_images(digest, None, version="1.2.3")[0] == digest
    with pytest.raises(cfg.ConfigError):
        resolve_images("not a reference", None)


def test_the_package_version_matches_the_control_plane_version():
    from quadringent.local.images import package_version_string
    from quadringent_control_plane.version import version

    assert package_version_string() == version()


def test_identifier_patterns_match_the_managed_product():
    from quadringent_control_plane import connections

    for name in ("IBMI_HOST", "IBMI_USER", "SNOWFLAKE_IDENTIFIER", "SNOWFLAKE_ACCOUNT"):
        assert getattr(cfg, name).pattern == getattr(connections, f"_{name}").pattern.strip("^$"), name


def test_the_documented_commands_and_the_sample_configuration_are_valid():
    import re
    import shlex
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sample = json.loads((root / "examples" / "local" / "config.sample.json").read_text())
    config = cfg.from_dict(sample)
    assert config.to_dict() == sample  # the sample is exactly what init writes
    text = (root / "docs" / "product" / "try-it-local.md").read_text()
    text += (root / "examples" / "local" / "README.md").read_text()
    blocks = re.findall(r"```sh\n(.*?)```", text, flags=re.S)
    commands = []
    for block in blocks:
        for command in re.sub(r"\\\n\s*", " ", block).splitlines():
            command = command.split("#", 1)[0].strip()
            if command.startswith("quadringent local") and "..." not in command:
                commands.append(shlex.split(command.split(">", 1)[0])[1:])
    assert len(commands) >= 10
    for argv in commands:
        _build_parser().parse_args(argv)  # a documented command line must parse
