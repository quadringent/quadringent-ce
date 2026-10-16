"""Snowflake setup script and key pair of a local instance.

The script is a host-side copy of
``quadringent_control_plane.v2.services.destinations._build_setup_script`` for
the ``copy_merge`` destination mode (a parity test keeps them identical apart
from the second header line). The CLI never connects to Snowflake with admin
credentials: an administrator runs the script, and the service role it creates
is the only identity the loader uses.
"""

from __future__ import annotations

import os
from pathlib import Path
import uuid

from ..installer.runner import CommandRunner
from .config import SNOWFLAKE_ACCOUNT, SNOWFLAKE_IDENTIFIER, ConfigError
from .secrets import write_private_file


class KeyGenerationError(RuntimeError):
    """The RSA key pair could not be created; the message never carries key material."""


def new_service_identities() -> tuple[str, str]:
    """Role and user names, in the QDT_ROLE_/QDT_SVC_ convention the loader requires."""
    return f"QDT_ROLE_{uuid.uuid4().hex[:8].upper()}", f"QDT_SVC_{uuid.uuid4().hex[:8].upper()}"


def public_key_body(public_key_pem: str) -> str:
    """The complete public key as one base64 line, as the managed setup script writes it."""
    # One base64 line: Snowflake truncates a key split across lines in the SQL value.
    delimiters = {"-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----"}
    lines = (line.strip() for line in public_key_pem.strip().splitlines())
    return "".join(line for line in lines if line not in delimiters)


def validated_scope(database: str, schema: str | None) -> tuple[str, str | None]:
    for name, value in (("database", database), ("schema", schema)):
        if name == "schema" and value is None:
            continue
        if not isinstance(value, str) or SNOWFLAKE_IDENTIFIER.fullmatch(value) is None:
            raise ConfigError(f"invalid Snowflake {name}")
    return database.upper(), schema.upper() if schema is not None else None


def build_setup_script(
    *,
    snowflake_account: str,
    role_name: str,
    user_name: str,
    public_key_pem: str,
    destination_database: str = "QUADRINGENT",
    destination_schema: str | None = None,
) -> str:
    if SNOWFLAKE_ACCOUNT.fullmatch(snowflake_account) is None:
        raise ConfigError("invalid Snowflake account identifier")
    if not role_name.startswith("QDT_ROLE_"):
        raise ConfigError("the Snowflake role must start with QDT_ROLE_")
    database, output_schema = validated_scope(destination_database, destination_schema)
    history_schema = output_schema or "RAW"
    mirror_schema = output_schema or "CURATED"
    warehouse_name = f"QDT_WH_{role_name[9:]}"
    key_body = public_key_body(public_key_pem)
    script = f"""-- Quadringent setup script (Snowflake account: {snowflake_account})
-- Generated on this machine; to be executed by a Snowflake administrator.
-- No Quadringent admin credentials are required or transmitted.

CREATE ROLE IF NOT EXISTS {role_name};

CREATE USER IF NOT EXISTS {user_name}
  TYPE = SERVICE
  RSA_PUBLIC_KEY = '{key_body}'
  DEFAULT_ROLE = {role_name};

GRANT ROLE {role_name} TO USER {user_name};

CREATE WAREHOUSE IF NOT EXISTS {warehouse_name}
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;

GRANT USAGE ON WAREHOUSE {warehouse_name} TO ROLE {role_name};

CREATE DATABASE IF NOT EXISTS {database};
CREATE SCHEMA IF NOT EXISTS {database}.{history_schema};
CREATE SCHEMA IF NOT EXISTS {database}.{mirror_schema};

GRANT USAGE ON DATABASE {database} TO ROLE {role_name};
"""
    script += "-- The service role creates and owns its history and mirror tables.\n"
    for schema in dict.fromkeys((history_schema, mirror_schema)):
        script += f"GRANT USAGE, CREATE TABLE ON SCHEMA {database}.{schema} TO ROLE {role_name};\n"
    reader_role = f"QDT_READ_{role_name[len('QDT_ROLE_'):]}"
    script += f"""
-- Read access for customer users (grant to them:
--   GRANT ROLE {reader_role} TO ROLE <your analysts' role>;)
CREATE ROLE IF NOT EXISTS {reader_role};
GRANT USAGE ON DATABASE {database} TO ROLE {reader_role};
GRANT USAGE ON SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
GRANT SELECT ON ALL TABLES IN SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
GRANT SELECT ON FUTURE TABLES IN SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
"""
    return script


def create_key_pair(private_path: Path, runner: CommandRunner) -> str:
    """Write a new RSA-2048 PKCS8 private key (0600) at ``private_path``; return the public PEM.

    Uses ``cryptography`` when installed, else the ``openssl`` binary, which writes
    the key file itself: key material never goes through an argument or a pipe.
    """
    if os.path.lexists(private_path):
        raise KeyGenerationError("a Snowflake key already exists for this instance")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        return _create_with_openssl(private_path, runner)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    write_private_file(private_path, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _create_with_openssl(private_path: Path, runner: CommandRunner) -> str:
    if runner.which("openssl") is None:
        raise KeyGenerationError("cannot create the Snowflake key pair: install openssl or pip install cryptography")
    generated = runner.run(("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(private_path)))
    if not generated.ok:
        raise KeyGenerationError("openssl could not create the Snowflake key")
    os.chmod(private_path, 0o600)
    public = runner.run(("openssl", "pkey", "-in", str(private_path), "-pubout"))
    if not public.ok or "BEGIN PUBLIC KEY" not in public.stdout:
        raise KeyGenerationError("openssl could not derive the Snowflake public key")
    return public.stdout
