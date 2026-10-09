import gzip
import io
from pathlib import Path
import tarfile
import pytest
from normalize_release_archive import entries, fingerprint, normalize


def fixture(path: Path, *, link=False):
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo('project/échantillon.txt')
        member.uid = 123; member.gid = 456
        member.uname = member.gname = 'Example Person'; member.mode = 0o755
        member.pax_headers = {'comment': 'Example Person', 'atime': '123'}
        data = b'content and license unchanged'; member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
        if link:
            member = tarfile.TarInfo('project/link'); member.type = tarfile.SYMTYPE
            member.linkname = '../outside'; archive.addfile(member)


def test_owner_pax_neutral_and_exact_content(tmp_path):
    archive = tmp_path / 'package.tar.gz'; fixture(archive)
    before = fingerprint(entries(archive)); normalize(archive, 123456)
    assert fingerprint(entries(archive)) == before
    member, data = entries(archive)[0]
    assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, '', '', 123456)
    assert set(member.pax_headers) <= {'path', 'size'}
    assert b'Example Person' not in gzip.decompress(archive.read_bytes())


def test_rejected_link_preserves_original(tmp_path):
    archive = tmp_path / 'package.tar.gz'; fixture(archive, link=True)
    original = archive.read_bytes()
    with pytest.raises(ValueError):
        normalize(archive, 123456)
    assert archive.read_bytes() == original
    assert list(tmp_path.iterdir()) == [archive]


def test_reproducible_epoch(tmp_path):
    first = tmp_path / 'first.tar.gz'; second = tmp_path / 'second.tar.gz'
    fixture(first); fixture(second)
    normalize(first, 123456); normalize(second, 123456)
    assert first.read_bytes() == second.read_bytes()
    normalize(first, 123456)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize('name', ['/absolute', '../escape', 'project/../escape'])
def test_traversal_refused(tmp_path, name):
    archive = tmp_path / 'package.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo(name); output.addfile(member, io.BytesIO())
    original = archive.read_bytes()
    with pytest.raises(ValueError):
        normalize(archive, 123456)
    assert archive.read_bytes() == original
