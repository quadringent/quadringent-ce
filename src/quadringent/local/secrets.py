"""Private files and the secrets of a local instance.

Secrets are files with mode 0600 inside directories with mode 0700. They are
never passed as command arguments, environment values of this process or
``--env-file`` content: containers get them as read-only file mounts.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import stat
from typing import Callable, TextIO


class PrivateFileError(OSError):
    """A path is a symbolic link, has the wrong type, owner or permission mode."""


def _check(path: Path, *, directory: bool) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PrivateFileError(f"{path.name}: missing") from None
    if stat.S_ISLNK(info.st_mode):
        raise PrivateFileError(f"{path.name}: refused, it is a symbolic link")
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise PrivateFileError(f"{path.name}: unexpected file type")
    if info.st_uid != os.getuid():
        raise PrivateFileError(f"{path.name}: not owned by the current user")
    if info.st_mode & 0o077:
        wanted = "0700" if directory else "0600"
        raise PrivateFileError(f"{path.name}: mode {stat.S_IMODE(info.st_mode):04o} is too open (expected {wanted})")
    return info


def check_private_dir(path: Path) -> None:
    _check(path, directory=True)


def check_private_file(path: Path) -> None:
    _check(path, directory=False)


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (0700) if absent; an existing one must already be private."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    check_private_dir(path)


def write_private_file(path: Path, data: bytes) -> None:
    """Create a new 0600 file; never follows a link and never overwrites."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def replace_private_file(path: Path, data: bytes) -> None:
    """Atomically replace (or create) a 0600 file through a private temporary file."""
    if path.is_symlink():
        raise PrivateFileError(f"{path.name}: refused, it is a symbolic link")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        write_private_file(temporary, data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_private_text(path: Path) -> str:
    check_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read()


def read_password(
    *,
    password_file: str | None,
    stdin: TextIO,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """Password from a file, piped stdin or a hidden terminal prompt; never an argument."""
    if password_file:
        source = Path(password_file).expanduser()
        if source.is_symlink() or not source.is_file():
            raise PrivateFileError("--ibmi-password-file must be a regular file, not a link")
        value = source.read_text(encoding="utf-8")
    elif not stdin.isatty():
        value = stdin.read()
    else:
        value = prompt("IBM i password (hidden): ")
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\n" in value:
        raise PrivateFileError("the IBM i password must be one non-empty line")
    return value
