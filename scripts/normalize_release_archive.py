"Normalize sdist metadata without changing distributed file contents."
from __future__ import annotations
import argparse
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


def entries(archive: Path) -> list[tuple[tarfile.TarInfo, bytes]]:
    result = []; names = set()
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            name = PurePosixPath(member.name)
            if (not member.name or name.is_absolute() or '..' in name.parts
                    or '\\' in member.name or member.name in names
                    or not (member.isfile() or member.isdir())):
                raise ValueError('unsafe or duplicate archive entry')
            names.add(member.name)
            stream = source.extractfile(member) if member.isfile() else None
            data = stream.read() if stream else b''
            if member.isfile() and len(data) != member.size:
                raise ValueError('truncated archive entry')
            result.append((member, data))
    if not result:
        raise ValueError('empty archive')
    return result


def fingerprint(values: list[tuple[tarfile.TarInfo, bytes]]) -> list[tuple[str, bytes, int, str]]:
    return [(m.name, m.type, m.mode, hashlib.sha256(data).hexdigest()) for m, data in values]


def normalize(archive: Path, epoch: int) -> None:
    if type(epoch) is not int or not 0 <= epoch <= 0xffffffff or archive.is_symlink():
        raise ValueError('invalid archive or epoch')
    original = entries(archive)
    descriptor, name = tempfile.mkstemp(prefix='.' + archive.name + '.', dir=archive.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                    for member, data in original:
                        # Nouveau TarInfo : aucune métadonnée PAX amont. PAX recrée
                        # seulement les extensions nécessaires (p. ex. nom Unicode).
                        clean = tarfile.TarInfo(member.name)
                        clean.type = member.type; clean.mode = member.mode
                        clean.uid = clean.gid = 0; clean.uname = clean.gname = ''
                        clean.mtime = epoch; clean.size = len(data) if member.isfile() else 0
                        import io
                        target.addfile(clean, io.BytesIO(data) if member.isfile() else None)
            output.flush(); os.fsync(output.fileno())
        normalized = entries(temporary)
        if fingerprint(normalized) != fingerprint(original):
            raise ValueError('archive content parity failed')
        for member, _ in normalized:
            if (member.uid or member.gid or member.uname or member.gname or member.mtime != epoch
                    or set(member.pax_headers) - {'path', 'size'}):
                raise ValueError('archive metadata not neutral')
        os.chmod(temporary, archive.stat().st_mode & 0o777)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--epoch', type=int, default=None)
    args = parser.parse_args()
    epoch = args.epoch
    if epoch is None:
        try:
            epoch = int(os.environ['SOURCE_DATE_EPOCH'])
        except (KeyError, ValueError):
            parser.error("--epoch or SOURCE_DATE_EPOCH is required")
    try:
        normalize(args.archive, epoch)
    except (OSError, ValueError, tarfile.TarError):
        parser.exit(1, "Normalization refused; original archive preserved.\\n")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
