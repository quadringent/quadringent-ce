"""Configuration of a local instance: ``~/.quadringent/local/<name>/config.json``.

The file holds no secret. Layout of the instance directory (0700):

  config.json                 this configuration (0600)
  secrets/                    ibmi_password, snowflake_key.p8 (0700 / 0600)
  snowflake-setup.sql         setup script for a Snowflake administrator (public key only)
  ibmi-ca.pem                 optional CA certificate trusted for the IBM i (public)
  data/                       shared data directory mounted at /data (0700); only with data_location "bind"
                              (with "volume" the data lives in the Docker named volume qdt-local-<name>-data)
  state/                      run.json, per-container env files, lock (0700)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .secrets import (
    PrivateFileError,
    check_private_dir,
    check_private_file,
    ensure_private_dir,
    read_private_text,
    replace_private_file,
)

FORMAT_VERSION = 1
MARKER_NAME = ".quadringent-local"
MARKER_FORMAT_VERSION = 1
RAW_PREFIX_ROOT = "raw"
DEFAULT_ROOT = Path("~/.quadringent/local")
DEFAULT_DATABASE = "QUADRINGENT"
DATA_BIND = "bind"  # a host directory bind-mounted at /data
DATA_VOLUME = "volume"  # a Docker named volume mounted at /data
DATA_LOCATIONS = (DATA_BIND, DATA_VOLUME)
VOLUME_LABEL = "io.quadringent.local"  # label value: the instance name
VOLUME_ID_LABEL = "io.quadringent.local.id"  # volumes only; value: the instance's store label (unique per init)

# Same patterns as quadringent_control_plane.connections (parity test).
NAME = re.compile(r"[a-z][a-z0-9-]{0,28}[a-z0-9]")
LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
IBMI_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
IBMI_USER = re.compile(r"[A-Z][A-Z0-9_]{0,29}")
IBMI_LIBRARY = re.compile(r"[A-Z$#@][A-Z0-9$#@_.]{0,9}")
SNOWFLAKE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")
SNOWFLAKE_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
# Docker image reference: optional registry with port, path, tag and/or sha256 digest.
IMAGE_REFERENCE = re.compile(
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?"
)
IBM_ZONE_TOKENS = frozenset({"IBM:QP0100CET"})


class ConfigError(ValueError):
    """The configuration is missing, malformed or unsafe."""


@dataclass(frozen=True)
class IbmiConfig:
    host: str
    user: str
    tls: bool = True
    allow_plaintext: bool = False
    ca_file: str | None = None  # file name inside the instance directory
    database_port: int | None = None
    signon_port: int | None = None
    command_port: int | None = None


@dataclass(frozen=True)
class SnowflakeConfig:
    account: str
    role: str
    user: str
    database: str = DEFAULT_DATABASE
    schema: str | None = None  # None: history in RAW and mirror in CURATED, like the managed product


@dataclass(frozen=True)
class LocalConfig:
    name: str
    label: str
    ibmi: IbmiConfig
    snowflake: SnowflakeConfig
    table_schema: str
    table_name: str
    capture_image: str
    loader_image: str
    source_id: str
    pipeline_id: str
    table_id: str
    source_time_zone: str | None = None
    from_checkout: str | None = None  # developer-only: repository bind-mounted over the images
    data_location: str = DATA_BIND
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "label": self.label,
            "created_at": self.created_at,
            "ibmi": {
                "host": self.ibmi.host, "user": self.ibmi.user, "tls": self.ibmi.tls,
                "allow_plaintext": self.ibmi.allow_plaintext, "ca_file": self.ibmi.ca_file,
                "database_port": self.ibmi.database_port, "signon_port": self.ibmi.signon_port,
                "command_port": self.ibmi.command_port,
            },
            "source_time_zone": self.source_time_zone,
            "from_checkout": self.from_checkout,
            "data_location": self.data_location,
            "table": {"schema": self.table_schema, "name": self.table_name},
            "snowflake": {
                "account": self.snowflake.account, "database": self.snowflake.database,
                "schema": self.snowflake.schema, "role": self.snowflake.role, "user": self.snowflake.user,
            },
            "images": {"capture": self.capture_image, "loader": self.loader_image},
            "ids": {"source": self.source_id, "pipeline": self.pipeline_id, "table": self.table_id},
        }

    @property
    def uses_volume(self) -> bool:
        return self.data_location == DATA_VOLUME

    @property
    def volume_name(self) -> str:
        """Docker named volume of the data directory (used only when ``data_location`` is ``volume``)."""
        return volume_name_for(self.name)

    @property
    def warehouse(self) -> str:
        return "QDT_WH_" + self.snowflake.role[len("QDT_ROLE_"):]

    @property
    def history_schema(self) -> str:
        return self.snowflake.schema or "RAW"

    @property
    def mirror_schema(self) -> str:
        return self.snowflake.schema or "CURATED"

    @property
    def journal_label(self) -> str:
        return f"{self.table_schema}.{self.table_name}"


@dataclass(frozen=True)
class LocalPaths:
    root: Path
    name: str

    @property
    def directory(self) -> Path:
        return self.root / self.name

    @property
    def config(self) -> Path:
        return self.directory / "config.json"

    @property
    def secrets(self) -> Path:
        return self.directory / "secrets"

    @property
    def ibmi_password(self) -> Path:
        return self.secrets / "ibmi_password"

    @property
    def snowflake_key(self) -> Path:
        return self.secrets / "snowflake_key.p8"

    @property
    def setup_script(self) -> Path:
        return self.directory / "snowflake-setup.sql"

    @property
    def ca_file(self) -> Path:
        return self.directory / "ibmi-ca.pem"

    @property
    def data(self) -> Path:
        return self.directory / "data"

    @property
    def marker(self) -> Path:
        return self.data / MARKER_NAME

    @property
    def state(self) -> Path:
        return self.directory / "state"

    @property
    def run_json(self) -> Path:
        return self.state / "run.json"

    @property
    def admission(self) -> Path:
        return self.state / "admission.json"

    @property
    def lock(self) -> Path:
        return self.state / "lock"

    def env_file(self, role: str) -> Path:
        return self.state / f"{role}.env"

    @property
    def console_snapshot(self) -> Path:
        return self.data / "console" / "capture.json"


def resolve_root(value: str | None) -> Path:
    return Path(value).expanduser() if value else DEFAULT_ROOT.expanduser()


def validate_name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ConfigError("instance name: 2 to 30 characters, lowercase letters, digits and dashes, starting with a letter")
    return value


def volume_name_for(instance: str) -> str:
    return f"qdt-local-{instance}-data"


def generate_label() -> str:
    return "qdt-" + uuid.uuid4().hex[:8]


def validate_source_time_zone(value: str) -> str:
    if value in IBM_ZONE_TOKENS:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError("source time zone: an IANA name such as Europe/Paris, or IBM:QP0100CET") from None
    return value


def validate(config: LocalConfig) -> LocalConfig:
    validate_name(config.name)
    if not LABEL.fullmatch(config.label):
        raise ConfigError("invalid data directory label")
    if not IBMI_HOST.fullmatch(config.ibmi.host):
        raise ConfigError("invalid IBM i host name")
    if not IBMI_USER.fullmatch(config.ibmi.user):
        raise ConfigError("invalid IBM i user: uppercase letters, digits and underscore")
    if config.ibmi.allow_plaintext == config.ibmi.tls:
        raise ConfigError("tls and allow_plaintext must be opposites")
    if not config.ibmi.tls and not config.source_time_zone:
        raise ConfigError("plaintext IBM i connections need --source-time-zone: the probe job requires TLS")
    for port in (config.ibmi.database_port, config.ibmi.signon_port, config.ibmi.command_port):
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("invalid IBM i port")
    for value, what in ((config.table_schema, "table library"), (config.table_name, "table name")):
        if not IBMI_LIBRARY.fullmatch(value):
            raise ConfigError(f"invalid {what}: at most 10 uppercase characters")
    if not SNOWFLAKE_ACCOUNT.fullmatch(config.snowflake.account):
        raise ConfigError("invalid Snowflake account identifier")
    for value, what in ((config.snowflake.database, "database"), (config.snowflake.schema, "schema")):
        if value is not None and not SNOWFLAKE_IDENTIFIER.fullmatch(value):
            raise ConfigError(f"invalid Snowflake {what}")
    if not config.snowflake.role.startswith("QDT_ROLE_"):
        raise ConfigError("the Snowflake role must start with QDT_ROLE_")
    for image in (config.capture_image, config.loader_image):
        if not IMAGE_REFERENCE.fullmatch(image):
            raise ConfigError("invalid image reference")
    if config.source_time_zone is not None:
        validate_source_time_zone(config.source_time_zone)
    if config.data_location not in DATA_LOCATIONS:
        raise ConfigError("data_location must be bind or volume")
    if config.from_checkout is not None and not Path(config.from_checkout).is_absolute():
        raise ConfigError("from_checkout must be an absolute path")
    return config


def from_dict(document: object) -> LocalConfig:
    try:
        if not isinstance(document, dict) or document.get("format_version") != FORMAT_VERSION:
            raise ConfigError("unsupported configuration format")
        ibmi, table, snowflake = document["ibmi"], document["table"], document["snowflake"]
        images, ids = document["images"], document["ids"]
        config = LocalConfig(
            name=document["name"], label=document["label"], created_at=document["created_at"],
            ibmi=IbmiConfig(
                host=ibmi["host"], user=ibmi["user"], tls=bool(ibmi["tls"]),
                allow_plaintext=bool(ibmi["allow_plaintext"]), ca_file=ibmi.get("ca_file"),
                database_port=ibmi.get("database_port"), signon_port=ibmi.get("signon_port"),
                command_port=ibmi.get("command_port"),
            ),
            snowflake=SnowflakeConfig(
                account=snowflake["account"], role=snowflake["role"], user=snowflake["user"],
                database=snowflake["database"], schema=snowflake.get("schema"),
            ),
            table_schema=table["schema"], table_name=table["name"],
            capture_image=images["capture"], loader_image=images["loader"],
            source_id=ids["source"], pipeline_id=ids["pipeline"], table_id=ids["table"],
            source_time_zone=document.get("source_time_zone"),
            from_checkout=document.get("from_checkout"),
            data_location=document.get("data_location", DATA_BIND),
        )
    except (KeyError, TypeError) as error:
        raise ConfigError(f"malformed configuration: {type(error).__name__}") from None
    return validate(config)


def load(paths: LocalPaths) -> LocalConfig:
    if not paths.directory.exists():
        raise ConfigError(f"no local instance {paths.name!r}: run quadringent local init first")
    try:
        check_private_dir(paths.directory)
        document = json.loads(read_private_text(paths.config))
    except PrivateFileError as error:
        raise ConfigError(str(error)) from None
    except ValueError:
        raise ConfigError("config.json is not valid JSON") from None
    return from_dict(document)


def save(paths: LocalPaths, config: LocalConfig) -> None:
    replace_private_file(paths.config, (json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n").encode())


def check_layout(paths: LocalPaths, *, secrets: bool = True, data: bool = True) -> None:
    """Refuse links and open permission modes on every private path of the instance.

    ``data=False`` skips the host data directory: with a Docker named volume there is none.
    """
    try:
        check_private_dir(paths.directory)
        if data:
            check_private_dir(paths.data)
        check_private_dir(paths.state)
        check_private_file(paths.config)
        if secrets:
            check_private_dir(paths.secrets)
            check_private_file(paths.ibmi_password)
            check_private_file(paths.snowflake_key)
    except PrivateFileError as error:
        raise ConfigError(str(error)) from None


def marker_document(label: str) -> bytes:
    return (json.dumps({"format_version": MARKER_FORMAT_VERSION, "label": label}, sort_keys=True) + "\n").encode()


def prepare_data_dir(paths: LocalPaths, label: str) -> None:
    """Create data/ with its marker, console/ and scratch/ (0700), or verify them."""
    from .secrets import write_private_file

    ensure_private_dir(paths.data)
    for child in ("console", "scratch"):
        ensure_private_dir(paths.data / child)
    if not paths.marker.exists() and not paths.marker.is_symlink():
        write_private_file(paths.marker, marker_document(label))
    try:
        document = json.loads(read_private_text(paths.marker))
    except ValueError:
        raise ConfigError("data directory marker is not valid JSON") from None
    if document != {"format_version": MARKER_FORMAT_VERSION, "label": label}:
        raise ConfigError("data directory marker does not match this instance")


def instance_exists(paths: LocalPaths) -> bool:
    return os.path.lexists(paths.directory)
