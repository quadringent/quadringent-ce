"""Source identity baked into an image; independent platform admission is separate."""

from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import re
import stat
import tomllib


_FORMAT = "quadringent-build-identity-v1"
_PATH = Path(__file__).with_name("_build_identity.json")


@dataclass(frozen=True)
class BuildIdentity:
    source_revision: str
    version: str

    def __post_init__(self):
        if (type(self.source_revision) is not str or re.fullmatch(r"[0-9a-f]{40}", self.source_revision) is None
                or type(self.version) is not str or re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,63}", self.version) is None):
            raise ValueError("invalid build identity")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate build identity field")
        result[key] = value
    return result


def read_build_identity(path=None):
    path = _PATH if path is None else Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("build identity is unreadable") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
            raise ValueError("invalid build identity file")
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("build identity changed during read")
    finally:
        os.close(descriptor)
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (type(record) is not dict or set(record) != {"format_version", "source_revision", "version"}
                or record["format_version"] != _FORMAT):
            raise ValueError("invalid build identity fields")
        return BuildIdentity(record["source_revision"], record["version"])
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("invalid build identity document") from None


def write_build_identity(revision, pyproject, target=None):
    target = _PATH if target is None else Path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError("build identity must be generated in a clean image")
    if revision == "":
        return
    with Path(pyproject).open("rb") as source:
        raw_project = source.read(65537)
    if len(raw_project) > 65536:
        raise ValueError("package metadata exceeds build identity budget")
    identity = BuildIdentity(revision, tomllib.loads(raw_project.decode("utf-8"))["project"]["version"])
    raw = (json.dumps({"format_version": _FORMAT, "source_revision": identity.source_revision,
                       "version": identity.version}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o444)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser(description="Bake the selected source revision and package version into the image")
    parser.add_argument("--revision", default="")
    parser.add_argument("--pyproject", default="/app/pyproject.toml")
    args = parser.parse_args()
    write_build_identity(args.revision, args.pyproject)


if __name__ == "__main__":
    main()
