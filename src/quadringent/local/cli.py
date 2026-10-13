"""``quadringent local``: argument parsing and command handlers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Callable, TextIO

from ..diagnostics.render import render_text
from ..installer.runner import CommandRunner
from . import config as cfg
from .check import CheckOptions, LocalChecker
from .datalocation import default_data_location
from .images import resolve_images
from .orchestrator import PLAINTEXT_WARNING, Starter, reset as reset_instance, stop as stop_instance
from .secrets import PrivateFileError, ensure_private_dir, read_password, write_private_file
from .snowflake_setup import KeyGenerationError, build_setup_script, create_key_pair, new_service_identities
from .status import collect, render

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
MAX_CA_BYTES = 262_144

HELP = """Copy one IBM i table to Snowflake from this machine with Docker (DEV/INT preview).

init            create an instance: configuration, secrets, Snowflake setup script
snowflake-script print the SQL script a Snowflake administrator runs once
check           diagnose prerequisites (--signon, --discover, --snowflake contact remote systems)
start           probe, discover, copy once, then run capture and loader containers
status          show containers, stages and the capture console snapshot
stop            stop the containers; checkpoints are kept
reset --yes     delete the data directory or volume and run state (config and secrets are kept)"""


def add_local_subcommand(subparsers) -> None:
    local = subparsers.add_parser("local", help="Run one IBM i table to Snowflake on this machine with Docker.",
                                  description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
                                  allow_abbrev=False)
    commands = local.add_subparsers(dest="local_command", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--name", default="default", help="Instance name (default: default)")
        parser.add_argument("--root", default=None, help=argparse.SUPPRESS)

    init = commands.add_parser("init", allow_abbrev=False, help="Create an instance: config, secrets, Snowflake setup script.")
    common(init)
    init.add_argument("--ibmi-host", required=True)
    init.add_argument("--ibmi-user", required=True)
    init.add_argument("--table", required=True, metavar="LIBRARY/TABLE", help="The single table to copy")
    init.add_argument("--snowflake-account", required=True, help="Account identifier, e.g. myorg-myaccount")
    init.add_argument("--snowflake-database", default=cfg.DEFAULT_DATABASE)
    init.add_argument("--snowflake-schema", default=None,
                      help="One schema for history and mirror (default: RAW and CURATED)")
    init.add_argument("--source-time-zone", default=None,
                      help="IANA zone of the IBM i journal timestamps; default: derived from QTIMZON by start")
    init.add_argument("--ibmi-ca-file", default=None, metavar="PEM", help="CA certificate that signs the IBM i certificate")
    init.add_argument("--ibmi-password-file", default=None, metavar="FILE",
                      help="Read the IBM i password from this file (else stdin when piped, else a hidden prompt)")
    init.add_argument("--allow-plaintext-ibmi", action="store_true",
                      help="Unencrypted connection (ports 8471/8475/8476 and 449); needs --source-time-zone")
    init.add_argument("--database-port", type=int, default=None)
    init.add_argument("--signon-port", type=int, default=None)
    init.add_argument("--command-port", type=int, default=None)
    init.add_argument("--capture-image", default=None, help="Capture image (default: the published image of this version)")
    init.add_argument("--loader-image", default=None, help="Control-plane image used by the loader")
    init.add_argument("--data-location", choices=cfg.DATA_LOCATIONS, default=None,
                      help="Where the data directory lives: bind (a host folder; default on Linux) or volume "
                           "(a Docker named volume; default on Docker Desktop and macOS)")
    init.add_argument("--from-checkout", default=None, metavar="REPO",
                      help="DEVELOPER ONLY, not reproducible: bind-mount this checkout over the images")

    script = commands.add_parser("snowflake-script", allow_abbrev=False, help="Print the Snowflake setup script (public key only).")
    common(script)

    check = commands.add_parser("check", allow_abbrev=False, help="Diagnose prerequisites (quadringent.diagnostic.v1).")
    common(check)
    check.add_argument("--signon", action="store_true", help="Sign on to the IBM i once in a container")
    check.add_argument("--discover", action="store_true", help="Read the catalog entry of the table (signs on)")
    check.add_argument("--snowflake", action="store_true", help="Connect to Snowflake as the service user")
    check.add_argument("--json", action="store_true")
    check.add_argument("--timeout", type=float, default=5.0)

    start = commands.add_parser("start", allow_abbrev=False, help="Copy the table once, then run capture and loader containers.")
    common(start)
    start.add_argument("--yes", action="store_true", help="Do not ask for confirmation (a summary is always printed)")

    stop = commands.add_parser("stop", allow_abbrev=False, help="Stop the containers; checkpoints are kept.")
    common(stop)

    status = commands.add_parser("status", allow_abbrev=False, help="Show containers, stages and the capture console snapshot.")
    common(status)
    status.add_argument("--json", action="store_true")
    status.add_argument("--logs", action="store_true", help="Include the last log lines of the containers")

    reset = commands.add_parser("reset", allow_abbrev=False, help="Delete the data directory (or volume) and run state.")
    common(reset)
    reset.add_argument("--yes", action="store_true", help="Required: this deletes the copied data")


def _split_table(value: str) -> tuple[str, str]:
    library, separator, table = value.upper().partition("/")
    if not separator or not library or not table or "/" in table:
        raise cfg.ConfigError("--table must look like LIBRARY/TABLE")
    return library, table


def _copy_ca(source: str, destination: Path) -> None:
    path = Path(source).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CA_BYTES:
        raise cfg.ConfigError("--ibmi-ca-file must be a regular PEM file of at most 256 KiB")
    data = path.read_bytes()
    if b"BEGIN CERTIFICATE" not in data:
        raise cfg.ConfigError("--ibmi-ca-file contains no PEM certificate")
    write_private_file(destination, data)


def _init(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO, stderr: TextIO, stdin: TextIO,
          prompt: Callable[[str], str] | None, host_platform: str | None = None) -> int:
    root = cfg.resolve_root(args.root)
    paths = cfg.LocalPaths(root, cfg.validate_name(args.name))
    if cfg.instance_exists(paths):
        print(f"Instance {args.name!r} already exists in {paths.directory}: choose another --name, "
              "or run reset --yes and delete that directory to start over (a new Snowflake script is then needed).",
              file=stderr)
        return EXIT_USAGE
    library, table = _split_table(args.table)
    capture_image, loader_image = resolve_images(args.capture_image, args.loader_image)
    checkout = None
    if args.from_checkout:
        checkout_path = Path(args.from_checkout).expanduser().resolve()
        if not (checkout_path / "src" / "quadringent" / "storage_backend.py").is_file():
            raise cfg.ConfigError("--from-checkout: not a Quadringent repository checkout")
        checkout = str(checkout_path)
    if args.data_location:
        data_location, reason = args.data_location, "from --data-location"
    else:
        data_location, reason = default_data_location(runner, host_platform)
    role, user = new_service_identities()
    plaintext = bool(args.allow_plaintext_ibmi)
    config = cfg.validate(cfg.LocalConfig(
        name=args.name, label=cfg.generate_label(),
        ibmi=cfg.IbmiConfig(host=args.ibmi_host, user=args.ibmi_user.upper(), tls=not plaintext,
                            allow_plaintext=plaintext, ca_file="ibmi-ca.pem" if args.ibmi_ca_file else None,
                            database_port=args.database_port, signon_port=args.signon_port,
                            command_port=args.command_port),
        snowflake=cfg.SnowflakeConfig(account=args.snowflake_account, role=role, user=user,
                                      database=args.snowflake_database.upper(),
                                      schema=args.snowflake_schema.upper() if args.snowflake_schema else None),
        table_schema=library, table_name=table, capture_image=capture_image, loader_image=loader_image,
        source_id=os.urandom(16).hex(), pipeline_id=os.urandom(16).hex(), table_id=os.urandom(16).hex(),
        source_time_zone=cfg.validate_source_time_zone(args.source_time_zone) if args.source_time_zone else None,
        from_checkout=checkout, data_location=data_location,
    ))
    password = read_password(password_file=args.ibmi_password_file, stdin=stdin,
                             **({"prompt": prompt} if prompt else {}))
    root.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        os.mkdir(paths.directory, 0o700)
        created = True
        ensure_private_dir(paths.secrets)
        ensure_private_dir(paths.state)
        if not config.uses_volume:
            cfg.prepare_data_dir(paths, config.label)
        write_private_file(paths.ibmi_password, password.encode("utf-8"))
        public_key = create_key_pair(paths.snowflake_key, runner)
        script_text = build_setup_script(
            snowflake_account=config.snowflake.account, role_name=config.snowflake.role,
            user_name=config.snowflake.user, public_key_pem=public_key,
            destination_database=config.snowflake.database, destination_schema=config.snowflake.schema)
        write_private_file(paths.setup_script, script_text.encode("utf-8"))
        if args.ibmi_ca_file:
            _copy_ca(args.ibmi_ca_file, paths.ca_file)
        cfg.save(paths, config)
    except BaseException:
        if created:
            shutil.rmtree(paths.directory, ignore_errors=True)
        raise
    if plaintext:
        print(PLAINTEXT_WARNING, file=stderr)
    if checkout:
        print("NOTE: --from-checkout is developer-only and not reproducible: the containers run your checkout, "
              "not the published code.", file=stderr)
    print(f"Instance {config.name!r} created in {paths.directory}", file=stdout)
    if config.uses_volume:
        print(f"Data location: Docker volume {config.volume_name} ({reason}); start creates it and its marker.", file=stdout)
    else:
        print(f"Data location: host folder {paths.data} ({reason}).", file=stdout)
    print("The IBM i password and the Snowflake private key are stored there as private files (0600).", file=stdout)
    print("", file=stdout)
    print("Next:", file=stdout)
    print(f"  1. quadringent local snowflake-script --name {config.name}   (give the output to a Snowflake administrator)", file=stdout)
    print(f"  2. quadringent local check --name {config.name} --signon --discover --snowflake", file=stdout)
    print(f"  3. quadringent local start --name {config.name}", file=stdout)
    return EXIT_OK


def _load(args: argparse.Namespace, stderr: TextIO) -> tuple[cfg.LocalPaths, cfg.LocalConfig] | None:
    paths = cfg.LocalPaths(cfg.resolve_root(args.root), cfg.validate_name(args.name))
    return paths, cfg.load(paths)


def run_local_command(
    args: argparse.Namespace,
    runner: CommandRunner,
    stdout: TextIO,
    *,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    uid: int | None = None,
    gid: int | None = None,
    connector: Callable | None = None,
    handshake: Callable | None = None,
    prompt: Callable[[str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    host_platform: str | None = None,
) -> int:
    stderr = stderr if stderr is not None else sys.stderr
    stdin = stdin if stdin is not None else sys.stdin
    if uid is None and not hasattr(os, "getuid"):
        print("Native Windows is not supported: run quadringent local inside WSL2, on the Linux file system "
              "(Docker Desktop with the WSL2 backend can be the engine).", file=stderr)
        return EXIT_USAGE
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    try:
        if args.local_command == "init":
            return _init(args, runner, stdout, stderr, stdin, prompt, host_platform)
        paths, config = _load(args, stderr)
        if args.local_command == "snowflake-script":
            stdout.write(cfg.read_private_text(paths.setup_script))
            return EXIT_OK
        if args.local_command == "check":
            if not config.ibmi.tls:
                print(PLAINTEXT_WARNING, file=stderr)
            checker = LocalChecker(config, paths, runner, uid=uid, gid=gid, connector=connector, handshake=handshake)
            report = checker.build(CheckOptions(args.signon, args.discover, args.snowflake, args.timeout))
            if args.json:
                print(json.dumps(report.to_dict(), indent=2), file=stdout)
            else:
                stdout.write(render_text(report).replace("Quadringent doctor:", "Quadringent local check:", 1))
                for table, commands in checker.cl_commands:
                    if commands:
                        print(f"\nCL commands for {table} (review them; run by the IBM i administrator):", file=stdout)
                        for command in commands:
                            print(f"  {command}", file=stdout)
            return report.exit_code()
        if args.local_command == "start":
            if not config.ibmi.tls:
                print(PLAINTEXT_WARNING, file=stderr)
            if config.from_checkout:
                print("NOTE: developer-only --from-checkout in use: not reproducible.", file=stderr)
            if confirm is None:
                confirm = _default_confirm(stdin, stdout)
            return Starter(config, paths, runner, stdout, stderr, uid=uid, gid=gid, confirm=confirm,
                           yes=args.yes).run()
        if args.local_command == "stop":
            return stop_instance(config, paths, runner, stdout, uid=uid, gid=gid)
        if args.local_command == "status":
            document = collect(config, paths, runner, uid=uid, gid=gid, logs=args.logs)
            if args.json:
                print(json.dumps(document, indent=2), file=stdout)
            else:
                stdout.write(render(document))
            return EXIT_FAILED if document["problem"] else EXIT_OK
        if args.local_command == "reset":
            if not args.yes:
                print("reset deletes the copied data and the run state: pass --yes to confirm.", file=stderr)
                return EXIT_USAGE
            return reset_instance(config, paths, runner, stdout, uid=uid, gid=gid)
    except (cfg.ConfigError, PrivateFileError, KeyGenerationError) as error:
        print(f"{error}", file=stderr)
        return EXIT_USAGE
    except OSError as error:
        print(f"{type(error).__name__}: {error.strerror or 'file system error'}", file=stderr)
        return EXIT_FAILED
    return EXIT_USAGE


def _default_confirm(stdin: TextIO, stdout: TextIO) -> Callable[[str], bool]:
    def confirm(question: str) -> bool:
        if not stdin.isatty():
            print("No terminal to confirm on: pass --yes to start without asking.", file=stdout)
            return False
        stdout.write(question)
        stdout.flush()
        return stdin.readline().strip().lower() in ("y", "yes")
    return confirm
