"""``docker run`` plans for every container of a local instance.

Every container runs with a read-only root file system, no capability, no new
privileges, bounded memory, CPU and processes, as the host user, and a
``/tmp`` tmpfs. Non-secret settings go through a 0600 ``--env-file``; the IBM i
password and the Snowflake private key are read-only file mounts that the
in-image wrapper (``local_runtime exec``) turns into environment values of the
child process only. No secret value ever appears in an argument or an env file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .config import LocalConfig, LocalPaths, RAW_PREFIX_ROOT
from .snowflake_setup import validated_scope

RUNTIME_MODULE = "quadringent_control_plane.v2.executor.local_runtime"
CAPTURE_PYTHON = "/opt/venv/bin/python"  # python of the capture image
LOADER_PYTHON = "python"  # python of the control-plane image

DATA_MOUNT = "/data"
SECRETS_MOUNT = "/run/secrets"
PASSWORD_TARGET = f"{SECRETS_MOUNT}/ibmi_password"
KEY_TARGET = f"{SECRETS_MOUNT}/snowflake_key.p8"
CA_TARGET = "/etc/quadringent/ibmi-ca/ca.pem"
ADMISSION_TARGET = "/run/quadringent/admission.json"
CONSOLE_TARGET = f"{DATA_MOUNT}/console/capture.json"

LABEL = "io.quadringent.local"
CONTAINER_PREFIX = "qdt-local"
RESTART_POLICY = "on-failure:3"
TMPFS = "/tmp:rw,size=512m"

ROLES_ONE_SHOT = ("fs-probe", "probe", "discover", "boundary", "admit", "copy", "verify")
ROLES_SERVICE = ("capture", "loader")

# (memory, cpus, pids) per role.
LIMITS: dict[str, tuple[str, str, str]] = {
    "capture": ("2g", "2", "512"),
    "copy": ("2g", "2", "512"),
    "loader": ("1g", "1", "256"),
}
DEFAULT_LIMITS = ("1g", "1", "256")


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool = True

    def argv(self) -> tuple[str, ...]:
        if "," in str(self.source) or "," in self.target:
            raise ValueError("mount paths must not contain a comma")
        spec = f"type=bind,source={self.source},target={self.target}"
        return ("--mount", spec + (",readonly" if self.read_only else ""))


@dataclass(frozen=True)
class ContainerSpec:
    role: str
    image: str
    entrypoint: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()

    @property
    def service(self) -> bool:
        return self.role in ROLES_SERVICE


@dataclass(frozen=True)
class RunPlan:
    name: str
    argv: tuple[str, ...]
    env_file: Path
    env_text: str


def container_name(instance: str, role: str) -> str:
    # Instance names hold no dot, so two instances can never produce the same name (dash would be ambiguous).
    return f"{CONTAINER_PREFIX}.{instance}.{role}"


def env_text(env: Mapping[str, str]) -> str:
    lines = []
    for key, value in sorted(env.items()):
        if "\n" in value or "\r" in value or "\0" in value or "=" in key or not key:
            raise ValueError(f"environment value {key} cannot be written to an env file")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


# Repository paths bind-mounted over the image paths by the developer-only --from-checkout option.
CHECKOUT_CAPTURE = (
    ("src/quadringent", "/app/quadringent"),
    ("src/quadringent_control_plane", "/app/quadringent_control_plane"),
    ("scripts/as400_continuous_capture.py", "/app/as400_continuous_capture.py"),
    ("scripts/as400_snapshot_publish.py", "/app/as400_snapshot_publish.py"),
    ("scripts/quadringent_initial_copy_job.py", "/app/quadringent_initial_copy_job.py"),
    ("scripts/quadringent_source_probe_job.py", "/app/quadringent_source_probe_job.py"),
    ("scripts/quadringent_table_discovery_job.py", "/app/quadringent_table_discovery_job.py"),
)
CHECKOUT_LOADER = (
    ("src/quadringent", "/app/src/quadringent"),
    ("src/quadringent_control_plane", "/app/src/quadringent_control_plane"),
    ("scripts/quadringent_destination_loader.py", "/app/quadringent_destination_loader.py"),
)
LOADER_ROLES = ("loader", "verify")


def checkout_mounts(config: LocalConfig, role: str) -> tuple[Mount, ...]:
    if not config.from_checkout:
        return ()
    root = Path(config.from_checkout)
    table = CHECKOUT_LOADER if role in LOADER_ROLES else CHECKOUT_CAPTURE
    return tuple(Mount(root / source, target) for source, target in table)


def run_plan(config: LocalConfig, paths: LocalPaths, spec: ContainerSpec, *, uid: int, gid: int) -> RunPlan:
    name = container_name(config.name, spec.role)
    memory, cpus, pids = LIMITS.get(spec.role, DEFAULT_LIMITS)
    argv: list[str] = ["docker", "run"]
    argv += ["-d", "--restart", RESTART_POLICY] if spec.service else ["--rm"]
    argv += [
        "--init", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", pids, "--memory", memory, "--cpus", cpus,
        "--tmpfs", TMPFS, "-e", "HOME=/tmp", "--user", f"{uid}:{gid}",
        "--label", f"{LABEL}={config.name}", "--label", f"{LABEL}.role={spec.role}",
        "--name", name,
    ]
    env_file = paths.env_file(spec.role)
    argv += ["--env-file", str(env_file)]
    for mount in (*spec.mounts, *checkout_mounts(config, spec.role)):
        argv += mount.argv()
    argv += ["--entrypoint", spec.entrypoint, spec.image, *spec.command]
    return RunPlan(name=name, argv=tuple(argv), env_file=env_file, env_text=env_text(spec.env))


# -- environment ---------------------------------------------------------------------------------------------


def storage_env(config: LocalConfig) -> dict[str, str]:
    return {
        "QUADRINGENT_STORAGE_BACKEND": "local",
        "AS400_RAW_BUCKET": config.label,
        "AS400_LOCAL_DATA_DIR": DATA_MOUNT,
    }


def ibmi_env(config: LocalConfig) -> dict[str, str]:
    ibmi = config.ibmi
    env = {
        "ISERIES_HOST": ibmi.host,
        "ISERIES_USER": ibmi.user,
        "QUADRINGENT_LOCAL_IBMI_PASSWORD_FILE": PASSWORD_TARGET,
    }
    if ibmi.tls:
        env["AS400_TLS"] = "true"
    else:
        # The engine's existing pair: plaintext is refused unless both values are present.
        env["AS400_TLS"] = "false"
        env["AS400_ALLOW_PLAINTEXT"] = "true"
    if ibmi.ca_file:
        env["AS400_TLS_CA_FILE"] = CA_TARGET
    for name, port in (("AS400_DATABASE_PORT", ibmi.database_port), ("AS400_SIGNON_PORT", ibmi.signon_port),
                       ("AS400_COMMAND_PORT", ibmi.command_port)):
        if port is not None:
            env[name] = str(port)
    return env


def snowflake_env(config: LocalConfig) -> dict[str, str]:
    database, schema = validated_scope(config.snowflake.database, config.snowflake.schema)
    return {
        "SNOWFLAKE_ACCOUNT": config.snowflake.account,
        "SNOWFLAKE_USER": config.snowflake.user,
        "SNOWFLAKE_ROLE": config.snowflake.role,
        "QUADRINGENT_DESTINATION_DATABASE": database,
        "QUADRINGENT_DESTINATION_SCHEMA": schema or "RAW",
        "QUADRINGENT_MIRROR_SCHEMA": schema or "CURATED",
        "QUADRINGENT_LOCAL_SNOWFLAKE_KEY_FILE": KEY_TARGET,
    }


def table_env(config: LocalConfig, time_zone: str | None, journal: tuple[str, str] | None = None) -> dict[str, str]:
    env = {"ISERIES_SCHEMA": config.table_schema, "ISERIES_TABLE": config.table_name}
    if time_zone:
        env["AS400_SOURCE_TIME_ZONE"] = time_zone
    if journal:
        env["AS400_JOURNAL_LIBRARY"], env["AS400_JOURNAL_NAME"] = journal
    return env


# -- mounts --------------------------------------------------------------------------------------------------


def _unlinked(path: Path) -> Path:
    """Absolute path of a private file or directory; a link at the last component is refused, never followed."""
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symbolic link: refusing to mount it")
    return path.resolve()


def data_mount(paths: LocalPaths) -> Mount:
    return Mount(_unlinked(paths.data), DATA_MOUNT, read_only=False)


def password_mount(paths: LocalPaths) -> Mount:
    return Mount(_unlinked(paths.ibmi_password), PASSWORD_TARGET)


def key_mount(paths: LocalPaths) -> Mount:
    return Mount(_unlinked(paths.snowflake_key), KEY_TARGET)


def ibmi_mounts(config: LocalConfig, paths: LocalPaths) -> tuple[Mount, ...]:
    mounts = [password_mount(paths)]
    if config.ibmi.ca_file:
        mounts.append(Mount(_unlinked(paths.ca_file), CA_TARGET))
    return tuple(mounts)


# -- containers ----------------------------------------------------------------------------------------------


def runtime_command(subcommand: str, *args: str) -> tuple[str, ...]:
    return ("-P", "-m", RUNTIME_MODULE, subcommand, *args)


def capture_script_command(script: str | None, *child: str) -> tuple[str, ...]:
    """``exec-capture`` runs a capture image script with the IBM i password in its environment only.

    Without ``script`` it runs the capture reader; the probe and discovery jobs are the same wrapper with
    ``--script`` (they read ``ISERIES_PASSWORD`` like the reader).
    """
    head = ("--script", script) if script else ()
    return runtime_command("exec-capture", *head, "--", *child)


def fs_probe_spec(config: LocalConfig, paths: LocalPaths) -> ContainerSpec:
    return ContainerSpec("fs-probe", config.capture_image, CAPTURE_PYTHON, runtime_command("fs-probe"),
                         storage_env(config), (data_mount(paths),))


def probe_spec(config: LocalConfig, paths: LocalPaths, *, port: int | None = None) -> ContainerSpec:
    child = ["--host", config.ibmi.host, "--user", config.ibmi.user]
    if port is not None:
        child += ["--port", str(port)]
    return ContainerSpec("probe", config.capture_image, CAPTURE_PYTHON,
                         capture_script_command("/app/quadringent_source_probe_job.py", *child),
                         ibmi_env(config), ibmi_mounts(config, paths))


def discover_spec(config: LocalConfig, paths: LocalPaths) -> ContainerSpec:
    child = ("--host", config.ibmi.host, "--user", config.ibmi.user, "--libraries", config.table_schema,
             "--search", config.table_name, "--limit", "50")
    return ContainerSpec("discover", config.capture_image, CAPTURE_PYTHON,
                         capture_script_command("/app/quadringent_table_discovery_job.py", *child),
                         ibmi_env(config), ibmi_mounts(config, paths))


def boundary_spec(config: LocalConfig, paths: LocalPaths, *, time_zone: str, journal: tuple[str, str]) -> ContainerSpec:
    env = {**ibmi_env(config), **table_env(config, time_zone, journal)}
    return ContainerSpec("boundary", config.capture_image, CAPTURE_PYTHON, runtime_command("boundary"),
                         env, ibmi_mounts(config, paths))


def admit_spec(config: LocalConfig, paths: LocalPaths, *, boundary_json: str, journal: tuple[str, str]) -> ContainerSpec:
    env = {**storage_env(config), "AS400_JOURNAL_LIBRARY": journal[0], "AS400_JOURNAL_NAME": journal[1]}
    command = runtime_command(
        "admit", "--boundary-json", boundary_json, "--source-id", config.source_id,
        "--pipeline-id", config.pipeline_id, "--table-id", config.table_id, "--raw-prefix-root", RAW_PREFIX_ROOT)
    return ContainerSpec("admit", config.capture_image, CAPTURE_PYTHON, command, env, (data_mount(paths),))


def copy_spec(config: LocalConfig, paths: LocalPaths, *, time_zone: str, journal: tuple[str, str]) -> ContainerSpec:
    """The copy handoff comes from the admission document; the runtime derives the job environment itself."""
    env = {**storage_env(config), **ibmi_env(config), **table_env(config, time_zone, journal)}
    command = runtime_command("copy", "--admission-file", ADMISSION_TARGET)
    mounts = (data_mount(paths), Mount(_unlinked(paths.admission), ADMISSION_TARGET), *ibmi_mounts(config, paths))
    return ContainerSpec("copy", config.capture_image, CAPTURE_PYTHON, command, env, mounts)


def capture_spec(config: LocalConfig, paths: LocalPaths, *, time_zone: str, capture_env: Mapping[str, str]) -> ContainerSpec:
    env = {
        **storage_env(config), **ibmi_env(config), **table_env(config, time_zone),
        "AS400_READER_TIMEOUT_SECONDS": "300",
        **capture_env,
    }
    command = capture_script_command(None, "--console-snapshot-path", CONSOLE_TARGET)
    return ContainerSpec("capture", config.capture_image, CAPTURE_PYTHON, command,
                         env, (data_mount(paths), *ibmi_mounts(config, paths)))


def loader_spec(config: LocalConfig, paths: LocalPaths, *, table_set_json: str) -> ContainerSpec:
    env = {
        **storage_env(config), **snowflake_env(config),
        "AS400_RAW_PREFIX": RAW_PREFIX_ROOT,
        "QUADRINGENT_LOADER_TABLE_SET_JSON": table_set_json,
        "QUADRINGENT_LOADER_POLL_SECONDS": "1",
        "QUADRINGENT_STREAMING_FLUSH_EACH_BATCH": "false",
        "QUADRINGENT_HISTORY_MODE": "sql",
    }
    return ContainerSpec("loader", config.loader_image, LOADER_PYTHON, runtime_command("exec-loader"),
                         env, (data_mount(paths), key_mount(paths)))


def verify_spec(config: LocalConfig, paths: LocalPaths) -> ContainerSpec:
    return ContainerSpec("verify", config.loader_image, LOADER_PYTHON, runtime_command("snowflake-verify"),
                         snowflake_env(config), (key_mount(paths),))
