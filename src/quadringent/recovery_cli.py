"""Export and restore explicitly selected native replay material."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping, Sequence, TextIO

from .checkpoint import _decode_checkpoint_json
from .recovery_bundle import (
    RecoveryBudgets,
    RecoverySelection,
    export_recovery_bundle,
    restore_recovery_bundle,
    verify_recovery_bundle,
)
from .storage_backend import StorageBackend

SELECTION_FORMAT = "quadringent-native-recovery-selection-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


class RecoveryInputError(ValueError):
    """A controlled CLI message without provider or object contents."""


def _add_operations(parser: argparse.ArgumentParser) -> None:
    operations = parser.add_subparsers(dest="recovery_operation", required=True)
    for operation in ("export", "verify", "restore"):
        command = operations.add_parser(operation, help=f"{operation.capitalize()} a private native replay bundle.")
        command.add_argument("--directory", required=True, help="Private bundle directory (export creates a new directory).")
        if operation != "export":
            command.add_argument("--expected-sha256", required=True, help="Original protected manifest SHA256.")
        if operation != "verify":
            command.add_argument("--selection", required=True, help="Private JSON with the original authority, RAW prefixes and checkpoint keys.")
            command.add_argument("--writers-stopped", action="store_true",
                                 help="Attest that ALL writers affecting this material are stopped; this command does not stop them.")


def add_recovery_subcommand(subparsers) -> None:
    parser = subparsers.add_parser("recovery", help="Back up or restore selected native replay material.")
    _add_operations(parser)


def _read_selection(path: Path) -> RecoverySelection:
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("regular selection file required")
            payload = handle.read(65537)
        if len(payload) > 65536:
            raise ValueError("byte budget")
        record = _decode_checkpoint_json(payload)
        if (set(record) != {"format_version", "authority", "raw_prefixes", "checkpoint_keys"}
                or record["format_version"] != SELECTION_FORMAT
                or type(record["raw_prefixes"]) is not list or type(record["checkpoint_keys"]) is not list):
            raise ValueError("selection format")
        selection = RecoverySelection(record["authority"], tuple(record["raw_prefixes"]), tuple(record["checkpoint_keys"]))
        budgets = RecoveryBudgets()
        if len(selection.raw_prefixes) > budgets.max_raw_prefixes or len(selection.checkpoint_keys) > budgets.max_checkpoints:
            raise ValueError("selection count budget")
        return selection
    except (OSError, ValueError, TypeError) as error:
        raise RecoveryInputError("cannot read a valid native recovery selection; check its format, scope and 64 KiB limit") from error


def run_recovery_command(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
                         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    environ = environ if environ is not None else os.environ
    try:
        operation = args.recovery_operation
        if operation != "verify" and not args.writers_stopped:
            raise RecoveryInputError("--writers-stopped is required after stopping every affected writer")
        if operation != "export" and _DIGEST.fullmatch(args.expected_sha256) is None:
            raise RecoveryInputError("expected manifest SHA256 must be 64 lowercase hexadecimal characters")
        directory = Path(args.directory)
        if operation == "verify":
            manifest = verify_recovery_bundle(directory, expected_sha256=args.expected_sha256)
            digest = args.expected_sha256
            selected = manifest["selection"]
            raw_count, checkpoint_count = len(selected["raw_prefixes"]), len(selected["checkpoint_keys"])
        else:
            selection = _read_selection(Path(args.selection))
            storage = StorageBackend.from_environment(environ)
            if operation == "export":
                receipt = export_recovery_bundle(directory, storage=storage, selection=selection,
                                                 writers_suspended=True)
            else:
                receipt = restore_recovery_bundle(directory, expected_sha256=args.expected_sha256,
                                                  storage=storage, selection=selection, writers_suspended=True)
            digest = receipt["manifest_sha256"]
            raw_count, checkpoint_count = len(selection.raw_prefixes), len(selection.checkpoint_keys)
        print(f"Native recovery {operation} complete; manifest SHA256: {digest}", file=stdout)
        print(f"Scope: full receiver authority, {raw_count} selected RAW prefixes, {checkpoint_count} selected checkpoints.", file=stdout)
        print("Writer suspension is operator-attested. This operation does not validate IBM i or resume writers.", file=stdout)
        print("PostgreSQL, secrets, fleet PVCs and Snowflake data require separate recovery procedures.", file=stdout)
        return 0
    except RecoveryInputError as error:
        print(f"Recovery refused: {error}", file=stderr)
    except Exception as error:
        # SDK errors can include credential-bearing context.
        print(f"Recovery failed ({type(error).__name__}); no success was reported. Keep writers stopped.", file=stderr)
    return 2


def run(argv: Sequence[str], *, environ: Mapping[str, str] | None = None,
        stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quadringent recovery", description=__doc__)
    _add_operations(parser)
    return run_recovery_command(parser.parse_args(argv), environ=environ, stdout=stdout, stderr=stderr)


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
