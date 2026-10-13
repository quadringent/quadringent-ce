"""``local start``, ``stop`` and ``reset``: the ordered, restartable sequence over ``docker run``.

Order of ``start``: lock, permissions, images, file system gate, source probe
(first IBM i sign-on), table discovery, confirmation, journal boundary,
admission, initial copy, capture container, loader container. A failed
authentication stops everything and is never retried. Completed stages are
recorded in ``state/run.json`` (no secret) so a second ``start`` resumes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping, TextIO

from ..installer.runner import CommandResult, CommandRunner
from . import dockerplan as plan
from .config import (
    ConfigError,
    LocalConfig,
    LocalPaths,
    check_layout,
    prepare_data_dir,
)
from .discovery import (
    JobOutputError,
    TableAssessment,
    assess_table,
    find_table,
    parse_discovery,
    parse_probe,
    result_line,
)
from .secrets import PrivateFileError, check_private_file, ensure_private_dir, replace_private_file
from .tableset import AdmissionError, capture_identity_env, dumps, loader_table_entry, parse_admission

RESULT_PREFIX = "quadringent_local_result="
RUN_FORMAT_VERSION = 1
PLAINTEXT_WARNING = (
    "WARNING: --allow-plaintext-ibmi is set. The IBM i password and the business data cross the "
    "network unencrypted. Use it only on a trusted, isolated network."
)


class StartError(RuntimeError):
    """A step failed; the message is safe to print."""


class Locked(StartError):
    pass


# -- run state -----------------------------------------------------------------------------------------------


def identity_fingerprint(config: LocalConfig) -> str:
    """Fields that a resumed run must not change under the recorded admission."""
    values = [config.label, config.ibmi.host, config.ibmi.user, config.table_schema, config.table_name,
              config.snowflake.account, config.snowflake.database, config.snowflake.schema or "",
              config.snowflake.role, config.source_id, config.pipeline_id, config.table_id]
    return hashlib.sha256(dumps(values).encode()).hexdigest()


def load_run_state(paths: LocalPaths) -> dict:
    if not os.path.lexists(paths.run_json):
        return {"format_version": RUN_FORMAT_VERSION, "stages": {}}
    try:
        check_private_file(paths.run_json)
        state = json.loads(paths.run_json.read_text(encoding="utf-8"))
    except (PrivateFileError, ValueError, OSError):
        raise StartError("state/run.json is unreadable or unsafe: run quadringent local reset --yes") from None
    if not isinstance(state, dict) or state.get("format_version") != RUN_FORMAT_VERSION or not isinstance(
            state.get("stages"), dict):
        raise StartError("state/run.json has an unknown format: run quadringent local reset --yes")
    return state


def save_run_state(paths: LocalPaths, state: Mapping[str, object]) -> None:
    replace_private_file(paths.run_json, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())


class InstanceLock:
    """Exclusive advisory lock on ``state/lock``; one ``start``/``stop``/``reset`` at a time."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor = -1

    def __enter__(self) -> "InstanceLock":
        import fcntl  # imported here: the host package must not need it at import time

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self._descriptor = os.open(self._path, flags, 0o600)
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._descriptor)
            self._descriptor = -1
            raise Locked("another quadringent local command is running for this instance") from None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


# -- docker --------------------------------------------------------------------------------------------------


class Docker:
    def __init__(self, runner: CommandRunner, config: LocalConfig, paths: LocalPaths, *, uid: int, gid: int) -> None:
        self.runner, self.config, self.paths, self.uid, self.gid = runner, config, paths, uid, gid

    def ready(self) -> None:
        if self.runner.which("docker") is None:
            raise StartError("docker is not installed or not on PATH")
        result = self.runner.run(("docker", "version", "--format", "{{.Server.Version}}"))
        if not result.ok:
            raise StartError("the Docker daemon does not answer: start Docker and run the command again")

    def ensure_image(self, image: str, out: TextIO) -> None:
        if self.runner.run(("docker", "image", "inspect", image)).ok:
            return
        print(f"Pulling {image} ...", file=out)
        if not self.runner.run(("docker", "pull", image)).ok:
            raise StartError(f"cannot pull {image}: check the image name, your network and your registry login")

    def _remove(self, name: str) -> None:
        self.runner.run(("docker", "rm", "-f", name))

    def run(self, spec: plan.ContainerSpec) -> CommandResult:
        run_plan = plan.run_plan(self.config, self.paths, spec, uid=self.uid, gid=self.gid)
        ensure_private_dir(self.paths.state)
        replace_private_file(run_plan.env_file, run_plan.env_text.encode())
        self._remove(run_plan.name)
        return self.runner.run(run_plan.argv)

    def one_shot(self, spec: plan.ContainerSpec, what: str) -> CommandResult:
        result = self.run(spec)
        if not result.ok:
            raise StartError(f"{what} failed: {failure_reason(result)}")
        return result

    def local_result(self, spec: plan.ContainerSpec, what: str) -> dict:
        result = self.one_shot(spec, what)
        try:
            return result_line(result.stdout, RESULT_PREFIX)
        except JobOutputError:
            raise StartError(f"{what} printed no result") from None

    def states(self) -> dict[str, str]:
        """Container name -> state, for every container of this instance."""
        result = self.runner.run((
            "docker", "ps", "-a", "--filter", f"label={plan.LABEL}={self.config.name}",
            "--format", "{{.Names}}\t{{.State}}",
        ))
        states: dict[str, str] = {}
        for line in result.stdout.splitlines() if result.ok else ():
            name, _, state = line.partition("\t")
            if name:
                states[name] = state.strip()
        return states

    def stop_all(self) -> list[str]:
        names = [plan.container_name(self.config.name, role) for role in (*plan.ROLES_SERVICE, *plan.ROLES_ONE_SHOT)]
        present = self.states()
        existing = [name for name in names if name in present]
        for name in existing:
            self.runner.run(("docker", "stop", "--time", "30", name))
            self.runner.run(("docker", "rm", name))
        return existing


def failure_reason(result: CommandResult) -> str:
    """Last non-empty stderr line of a container, truncated; containers print redacted reasons only."""
    for line in reversed((result.stderr or result.stdout or "").splitlines()):
        if line.strip() and not line.startswith(RESULT_PREFIX):
            return line.strip()[:300]
    return f"exit code {result.returncode}"


# -- start ---------------------------------------------------------------------------------------------------


Confirm = Callable[[str], bool]


class Starter:
    def __init__(self, config: LocalConfig, paths: LocalPaths, runner: CommandRunner, out: TextIO, err: TextIO, *,
                 uid: int, gid: int, confirm: Confirm, yes: bool) -> None:
        self.config, self.paths, self.out, self.err = config, paths, out, err
        self.docker = Docker(runner, config, paths, uid=uid, gid=gid)
        self.confirm, self.yes = confirm, yes

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    # -- stages

    def run(self) -> int:
        config, paths = self.config, self.paths
        try:
            check_layout(paths)
            prepare_data_dir(paths, config.label)
            if config.ibmi.allow_plaintext:
                print(PLAINTEXT_WARNING, file=self.err)
            with InstanceLock(paths.lock):
                self._start_locked()
        except (StartError, ConfigError, PrivateFileError, AdmissionError, ValueError, OSError) as error:
            print(f"Start stopped: {error}", file=self.err)
            return 1
        return 0

    def _start_locked(self) -> None:
        state = load_run_state(self.paths)
        fingerprint = identity_fingerprint(self.config)
        stages: dict = state["stages"]
        if stages.get("identity") not in (None, fingerprint):
            raise StartError("the configuration changed since the recorded run: run quadringent local reset --yes")
        self.docker.ready()
        self.say("[1/8] Images")
        for image in dict.fromkeys((self.config.capture_image, self.config.loader_image)):
            self.docker.ensure_image(image, self.out)
        self.say("[2/8] Data directory file system check")
        self._fs_probe()
        if "identity" in stages and "table" in stages:
            self.say("[3/8] Resuming: probe, discovery and confirmation are already recorded (no new sign-on for them)")
        else:
            self._probe_and_discover(state, stages)
            self._confirm(stages)
            stages["identity"] = fingerprint
            save_run_state(self.paths, state)
        self._admit_and_copy(state, stages)
        self.say("[7/8] Capture container")
        self._start_service(self._capture_spec(stages))
        self.say("[8/8] Loader container")
        self._start_service(self._loader_spec(stages))
        self.say("")
        self.say("Running. Follow it with: quadringent local status --logs")
        self.say("Stop with: quadringent local stop (checkpoints are kept; start resumes).")

    def _fs_probe(self) -> None:
        result = self.docker.run(plan.fs_probe_spec(self.config, self.paths))
        try:
            document = result_line(result.stdout, RESULT_PREFIX)
        except JobOutputError:
            raise StartError(f"the file system check printed no result: {failure_reason(result)}") from None
        if result.ok and document.get("ok") is True:
            return
        checks = document.get("checks") if isinstance(document.get("checks"), dict) else {}
        failed = sorted(name for name, value in checks.items() if isinstance(value, dict) and value.get("ok") is False)
        raise StartError(
            "the data directory is on an unsupported file system"
            + (f" (failed: {', '.join(failed)})" if failed else "")
            + ". Docker must mount a local Linux disk (ext4, xfs, btrfs) that grants file locks between containers; "
            "not NFS, SMB, FUSE, a cloud-synced folder, FAT/exFAT, tmpfs, /mnt/c under WSL2 or a Docker Desktop "
            "shared folder on macOS or Windows (run on a Linux host or in a Linux VM)."
        )

    def _probe_and_discover(self, state: dict, stages: dict) -> None:
        config = self.config
        time_zone = config.source_time_zone
        if config.ibmi.tls:
            self.say("[3/8] Source probe (first IBM i sign-on, read-only)")
            result = self.docker.run(plan.probe_spec(config, self.paths, port=config.ibmi.signon_port))
            try:
                probe = parse_probe(result.stdout)
            except JobOutputError:
                raise StartError(f"the source probe printed no result: {failure_reason(result)}") from None
            if not probe.network_ok:
                raise StartError(f"IBM i unreachable: {probe.network_detail}")
            if probe.tls_trust == "unknown" or not probe.tls_ok:
                raise StartError(
                    "the IBM i certificate is not trusted"
                    + (f" (SHA-256 {probe.tls_fingerprint})" if probe.tls_fingerprint else "")
                    + ": pass --ibmi-ca-file with the certificate authority of the IBM i at init. Verification is never disabled."
                )
            if not probe.auth_ok:
                raise StartError(
                    f"IBM i sign-on refused ({probe.auth_detail}). Nothing else was attempted and nothing "
                    "will be retried: fix the user or password with the IBM i administrator, then run start again."
                )
            time_zone = time_zone or probe.detected_timezone
            if probe.detected_timezone and config.source_time_zone and probe.detected_timezone != config.source_time_zone:
                print(f"Note: the IBM i reports {probe.qtimzon}, mapped to {probe.detected_timezone}; "
                      f"keeping the configured {config.source_time_zone}.", file=self.err)
            stages["probe"] = {"ibmi_version": probe.ibmi_version, "qtimzon": probe.qtimzon,
                               "detected_timezone": probe.detected_timezone, "tls_fingerprint": probe.tls_fingerprint}
        else:
            self.say("[3/8] Source probe skipped (plaintext mode: the probe job requires TLS)")
        if not time_zone:
            raise StartError(
                "the source time zone could not be derived from the IBM i value QTIMZON: "
                "set it with --source-time-zone at init (an IANA name such as Europe/Paris)."
            )
        stages["time_zone"] = time_zone
        self.say("[4/8] Table discovery (catalog only, no business row is read)")
        result = self.docker.one_shot(plan.discover_spec(config, self.paths), "table discovery")
        try:
            tables = parse_discovery(result.stdout)
        except JobOutputError as error:
            raise StartError(str(error)) from None
        table = find_table(tables, config.table_schema, config.table_name)
        if table is None:
            raise StartError(f"table {config.table_schema}/{config.table_name} was not found on the IBM i "
                             "(or the user cannot see it)")
        assessment = assess_table(table)
        if not assessment.ready:
            self._print_not_ready(assessment)
            raise StartError("the table is not ready; nothing was changed on the IBM i")
        stages["table"] = {
            "journal_library": assessment.journal_library, "journal_name": assessment.journal_name,
            "key_columns": list(assessment.key_columns), "columns": [dict(c) for c in assessment.columns],
        }
        save_run_state(self.paths, state)

    def _print_not_ready(self, assessment: TableAssessment) -> None:
        for reason in assessment.reasons:
            print(reason, file=self.err)
        commands = assessment.cl_commands()
        if commands:
            print("Ask the IBM i administrator to review and run (this tool never changes the IBM i):", file=self.err)
            for command in commands:
                print(f"  {command}", file=self.err)

    def _confirm(self, stages: dict) -> None:
        config, table = self.config, stages["table"]
        lines = [
            "",
            f"Quadringent local instance {config.name!r}",
            f"  IBM i      {config.ibmi.host} as {config.ibmi.user}, table {config.table_schema}/{config.table_name}, "
            f"journal {table['journal_library']}/{table['journal_name']}, key {', '.join(table['key_columns'])}",
            f"  Connection {'TLS with certificate verification' if config.ibmi.tls else 'PLAINTEXT (not encrypted)'}",
            f"  Time zone  {stages['time_zone']}",
            f"  Snowflake  account {config.snowflake.account}, database {config.snowflake.database}, "
            f"schemas {config.history_schema} (history) and {config.mirror_schema} (mirror), role {config.snowflake.role}",
            f"  Warehouse  {config.warehouse} (XSMALL, auto-suspend 60 s): it bills while loading",
            f"  Data       {self.paths.data} - holds the table rows in CLEAR TEXT, protect this folder",
            f"  Images     {config.capture_image}",
            f"             {config.loader_image}",
            "On the IBM i: read-only (catalog, journal receivers, one table snapshot). In Snowflake: the service",
            "role creates and writes the history and mirror tables of this one table.",
        ]
        for line in lines:
            self.say(line)
        if self.yes:
            return
        if not self.confirm("Continue? [y/N] "):
            raise StartError("not confirmed: nothing was copied")

    def _admit_and_copy(self, state: dict, stages: dict) -> None:
        config, paths = self.config, self.paths
        table, time_zone = stages["table"], stages["time_zone"]
        journal = (table["journal_library"], table["journal_name"])
        if "admission" in stages:
            self.say("[5/8] Admission already recorded")
        else:
            self.say("[5/8] Journal boundary and admission")
            boundary = self.docker.local_result(
                plan.boundary_spec(config, paths, time_zone=time_zone, journal=journal), "reading the journal boundary")
            admitted = self.docker.local_result(
                plan.admit_spec(config, paths, boundary_json=dumps(boundary), journal=journal), "admission")
            stages["admission"] = parse_admission(admitted, table_id=config.table_id, pipeline_id=config.pipeline_id)
            save_run_state(paths, state)
        admission = stages["admission"]
        if "copy" not in stages:
            self.say("[6/8] Initial copy (one read-only snapshot of the table)")
            replace_private_file(paths.admission, dumps(admission).encode())
            result = self.docker.local_result(
                plan.copy_spec(config, paths, time_zone=time_zone, journal=journal), "the initial copy")
            stages["copy"] = {"rows": result.get("rows"), "evidence_key": result.get("evidence_key")}
            save_run_state(paths, state)
            self.say(f"      copied {result.get('rows')} rows")
        else:
            self.say("[6/8] Initial copy already done")

    def _capture_spec(self, stages: dict) -> plan.ContainerSpec:
        table = stages["table"]
        env = capture_identity_env(stages["admission"], table=self.config.table_name,
                                   journal_library=table["journal_library"], journal_name=table["journal_name"])
        return plan.capture_spec(self.config, self.paths, time_zone=stages["time_zone"], capture_env=env)

    def _loader_spec(self, stages: dict) -> plan.ContainerSpec:
        table = stages["table"]
        entry = loader_table_entry(stages["admission"], schema=self.config.table_schema, table=self.config.table_name,
                                   key_columns=tuple(table["key_columns"]), columns=tuple(table["columns"]))
        return plan.loader_spec(self.config, self.paths, table_set_json=dumps([entry]))

    def _start_service(self, spec: plan.ContainerSpec) -> None:
        name = plan.container_name(self.config.name, spec.role)
        if self.docker.states().get(name) == "running":
            self.say(f"      {name} is already running")
            return
        result = self.docker.run(spec)
        if not result.ok:
            raise StartError(f"cannot start {name}: {failure_reason(result)}")
        self.say(f"      {name} started")


# -- stop / reset --------------------------------------------------------------------------------------------


def stop(config: LocalConfig, paths: LocalPaths, runner: CommandRunner, out: TextIO, *, uid: int, gid: int) -> int:
    docker = Docker(runner, config, paths, uid=uid, gid=gid)
    try:
        docker.ready()
        with InstanceLock(paths.lock):
            stopped = docker.stop_all()
    except (StartError, OSError) as error:
        print(f"Stop failed: {error}", file=out)
        return 1
    print(f"Stopped {len(stopped)} container(s). Checkpoints and data are kept; start resumes.", file=out)
    return 0


def reset(config: LocalConfig, paths: LocalPaths, runner: CommandRunner, out: TextIO, *, uid: int, gid: int) -> int:
    """Stop, then delete the data directory and run state. Config, secrets and Snowflake objects stay."""
    docker = Docker(runner, config, paths, uid=uid, gid=gid)
    try:
        docker.ready()
        check_layout(paths)
        with InstanceLock(paths.lock):
            docker.stop_all()
            if paths.data.is_symlink() or not paths.marker.is_file():
                raise StartError("the data directory has no Quadringent marker: refusing to delete it")
            shutil.rmtree(paths.data)
            for leftover in (paths.run_json, paths.admission, *(paths.env_file(role) for role in (*plan.ROLES_SERVICE, *plan.ROLES_ONE_SHOT))):
                leftover.unlink(missing_ok=True)
            prepare_data_dir(paths, config.label)
    except (StartError, ConfigError, PrivateFileError, OSError) as error:
        print(f"Reset failed: {error}", file=out)
        return 1
    print("Data directory and run state deleted. Snowflake tables are not touched: drop them yourself "
          "before copying the same table again.", file=out)
    return 0
