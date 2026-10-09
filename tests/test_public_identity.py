import json
from pathlib import Path
import subprocess
import pytest
from check_public_identity import load_policy, review, scan_identity


def policy(tmp_path):
    p = tmp_path / 'policy.json'
    p.write_text(json.dumps({'identities': ['xy', 'Example Person']}))
    p.chmod(0o600)
    return p


def test_literal_boundaries_and_case(tmp_path):
    patterns = load_policy(policy(tmp_path), tmp_path / 'source')
    assert not scan_identity('proxy xylophone abcxy123', 'file/1', patterns)
    assert len(scan_identity('XY; example person', 'file/1', patterns)) == 1
    assert scan_identity('example personality', 'file/1', patterns)


def test_private_policy_required(tmp_path):
    p = policy(tmp_path)
    with pytest.raises(ValueError):
        load_policy(p, tmp_path)
    p.chmod(0o644)
    with pytest.raises(ValueError):
        load_policy(p, tmp_path / 'source')


def test_clean_tree_does_not_hide_old_identity(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
    (root / 'document').write_text('Example Person')
    git('add', '.'); git('commit', '-m', 'initial')
    (root / 'document').write_text('neutral')
    git('add', '.'); git('commit', '-m', 'neutral')
    p = policy(tmp_path)
    assert not review(root, p)
    findings = review(root, p, 'HEAD')
    assert any(f.rule == 'private_identity' for f in findings)
    assert all('Example Person' not in str(f) for f in findings)


def test_explicit_modes_and_filename_separators(tmp_path):
    p = policy(tmp_path)
    p.write_text(json.dumps({'identities': [
        {'value': 'Examplevendor', 'mode': 'substring'},
        {'value': 'xy', 'mode': 'lexical'}]}))
    patterns = load_policy(p, tmp_path / 'source')
    assert scan_identity('neutrallegacy_examplevendor.py', 'file/1', patterns)
    assert scan_identity('xy_foo', 'file/1', patterns)
    assert scan_identity('XY/path', 'file/1', patterns)
    assert not scan_identity('proxy xylophone 123xy456', 'file/1', patterns)
    p.write_text(json.dumps({'identities': [{'value': 'xy', 'mode': 'substring'}]}))
    with pytest.raises(ValueError):
        load_policy(p, tmp_path / 'source')


def png(*metadata):
    import zlib
    import struct
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + b''.join(chunk(kind, data) for kind, data in metadata)
            + chunk(b'IDAT', b'compressed_random_xy_bytes') + chunk(b'IEND', b''))


def test_png_pixels_metadata_and_malformed_fail_closed(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    subprocess.run(['git', 'init'], cwd=root, check=True, capture_output=True)
    p = policy(tmp_path)
    image = root / 'neutral.png'
    image.write_bytes(png())
    assert not review(root, p)
    image.write_bytes(png((b'tEXt', b'Author\x00xy')))
    assert any(f.rule == 'private_identity' for f in review(root, p))
    image.write_bytes(png()[:-1])
    assert any(f.rule == 'png_unscanned' for f in review(root, p))
    image.write_bytes(b'ordinary_binary\x00xy\x00')
    assert any(f.rule == 'private_identity' for f in review(root, p))


def test_old_png_metadata_git_blob_not_hidden(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    def git(*args):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
    image = root / 'neutral.png'; image.write_bytes(png((b'tEXt', b'Author\x00xy')))
    git('add', '.'); git('commit', '-m', 'initial')
    image.write_bytes(png()); git('add', '.'); git('commit', '-m', 'neutral')
    p = policy(tmp_path)
    assert not review(root, p)
    assert any(f.rule == 'private_identity' for f in review(root, p, 'HEAD'))


def test_compressed_png_text_bounded_and_generic_gate_unchanged(tmp_path):
    import zlib
    from check_public_identity import bounded_inflate, png_metadata
    from check_publication import scan_payload
    assert b'xy' in png_metadata(png((b'zTXt', b'Author\x00\x00' + zlib.compress(b'xy'))))
    with pytest.raises(ValueError):
        bounded_inflate(zlib.compress(b'a' * (16 * 1024 * 1024 + 1)))
    assert any(f.rule == 'local_home_path' for f in scan_payload(b'/' + b'Users/example/private', 'file/1'))
