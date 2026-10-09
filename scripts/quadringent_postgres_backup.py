#!/usr/bin/env python3
"Back up control-plane PostgreSQL to the site's storage and verify readback."

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile

from quadringent.storage_backend import StorageBackend

ENV_DSN = "QUADRINGENT_PG_BACKUP_DSN"
ENV_PREFIX = "QUADRINGENT_PG_BACKUP_PREFIX"
DEFAULT_PREFIX = "postgres-backups"


def _require(name: str, environ: dict[str, str]) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"{name} is required for PostgreSQL backup")
    return value


def dump(dsn: str, destination: Path) -> None:
    """Exécute ``pg_dump`` en format custom (compressé, restaurable par ``pg_restore``)."""

    subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(destination), dsn],
        check=True,
    )


def validate_archive(content: bytes, validation_file: Path | None = None) -> str:
    """Vérifie le format, puis décode toute l'archive si pg_restore est disponible.

    Sans outil local, un reçu lié aux octets de l'initContainer est obligatoire.
    Ce reçu atteste un décodage local, sans authentification indépendante.
    Le décodage SQL ne se connecte à aucune base et ne restaure aucune donnée.
    """
    if len(content) <= 5 or not content.startswith(b"PGDMP"):
        raise ValueError("Postgres archive is empty or is not in custom format")
    executable = shutil.which("pg_restore")
    if executable is None:
        if validation_file is None:
            raise ValueError("pg_restore validation or local receipt required")
        # Reçu de l'initContainer de confiance ; aucune authentification indépendante.
        try:
            with validation_file.open("rb") as handle:
                encoded = handle.read(4097)
            if len(encoded) > 4096:
                raise ValueError("receipt too large")
            receipt = json.loads(encoded)
        except (OSError, ValueError) as error:
            raise ValueError("validation receipt unreadable") from error
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"schema", "validator", "bytes", "sha256"}
            or receipt["schema"] != "quadringent.pg-backup-validation.v1"
            or receipt["validator"] != "pg_restore-full-sql-decode"
            or type(receipt["bytes"]) is not int
            or receipt["bytes"] != len(content)
            or receipt["sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("validation receipt does not match the archive bytes")
        return "recu-initContainer-decode-pg_restore-sans-restauration"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "archive.dump"
        path.write_bytes(content)
        try:
            subprocess.run(
                [executable, "--file", os.devnull, str(path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as from_error:
            raise ValueError("PostgreSQL archive refused by pg_restore") from from_error
    return "decode-pg_restore-sans-restauration"


def backup_filename(*, now: datetime | None = None) -> str:
    """Nom d'objet unique (horodaté) — relatif au préfixe de l'object store."""

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"quadringent-postgres-{stamp}.dump"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump-file",
        default="",
        help="file already produced by pg_dump (dedicated initContainer); otherwise run pg_dump",
    )
    parser.add_argument(
        "--validation-file", default="",
        help="private pg_restore decoding receipt produced by the trusted initContainer",
    )
    arguments = parser.parse_args(argv)
    if arguments.validation_file and not arguments.dump_file:
        parser.error("--validation-file exige --dump-file")
    environ = os.environ
    prefix = (environ.get(ENV_PREFIX) or "").strip() or DEFAULT_PREFIX

    if arguments.dump_file:
        content = Path(arguments.dump_file).read_bytes()
    else:
        dsn = _require(ENV_DSN, environ)
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "postgres.dump"
            dump(dsn, dump_path)
            content = dump_path.read_bytes()

    validation = validate_archive(content, Path(arguments.validation_file) if arguments.validation_file else None)
    backend = StorageBackend.from_environment(environ)
    store = backend.object_store(prefix)
    filename = backup_filename()
    store.put_once(filename, content)
    expected_hash = hashlib.sha256(content).hexdigest()
    observed = store.get_bounded(filename, len(content))
    if len(observed) != len(content) or hashlib.sha256(observed).hexdigest() != expected_hash:
        raise ValueError("backup readback mismatch")
    print(
        f"backup published and reread: {prefix}/{filename} "
        f"({len(content)} octets, sha256={expected_hash}, validation={validation})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
