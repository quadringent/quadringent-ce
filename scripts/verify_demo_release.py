"""Verify and extract the synthetic demo bound to a release commit."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def verify_demo_release(archive: Path, destination: Path, *, version: str, source_commit: str) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Invalid release version")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Invalid release commit")
    name = f"quadringent-demo-{version}"
    if archive.name != f"{name}.tar.gz":
        raise ValueError("Unexpected demo archive name")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Extraction destination must be new")
    checksum = Path(str(archive) + ".sha256").read_text().strip()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum != f"{digest}  {archive.name}":
        raise ValueError("Demo archive checksum mismatch")

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        seen = set()
        if len(members) > 2000 or sum(member.size for member in members) > 64 * 1024 * 1024:
            raise ValueError("Demo archive exceeds release limits")
        for member in members:
            path = PurePosixPath(member.name)
            canonical = str(path)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != name or canonical in seen
                    or member.name.rstrip("/") != canonical
                    or (member.isfile() and member.name != canonical)
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe or duplicate demo archive entry")
            if (member.uid or member.gid or member.uname or member.gname or member.pax_headers
                    or any(part.startswith("._") or part == ".DS_Store" for part in path.parts)):
                raise ValueError("Non-neutral demo archive metadata")
            seen.add(canonical)
        required = {"PROVENANCE.json", "LICENSE", "NOTICE", "third-party-notices.txt",
                    "index.html", "serve-demo.mjs", "README.txt"}
        files = {member.name for member in members if member.isfile() and member.size > 0}
        if not {f"{name}/{item}" for item in required}.issubset(files):
            raise ValueError("Demo archive is missing provenance, licenses or runtime files")
        stream = tar.extractfile(f"{name}/PROVENANCE.json")
        if stream is None:
            raise ValueError("Demo provenance is not a regular file")
        provenance = json.load(stream)
        expected = {"product": "Quadringent Community", "kind": "synthetic-demo",
                    "version": version, "sourceCommit": source_commit,
                    "sourceDirty": False, "synthetic": True, "connections": "none"}
        if any(provenance.get(key) != value or type(provenance.get(key)) is not type(value)
               for key, value in expected.items()):
            raise ValueError("Demo archive does not match the clean release commit")
        destination.mkdir(parents=True)
        tar.extractall(destination, filter="data")
    return destination / name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    print(verify_demo_release(args.archive, args.output, version=args.version,
                              source_commit=args.source_commit))


if __name__ == "__main__":
    main()
