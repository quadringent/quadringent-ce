"""Collection archives retain original bytes without certifying native admission."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2.services.native_artifacts import (
    ArchiveLimits, ArtifactArchive, NativeArtifactError,
)


@pytest.fixture
def storage(tmp_path):
    root = tmp_path / 'durable'
    root.mkdir()
    (root / '.quadringent-local').write_text(json.dumps({'format_version': 1, 'label': 'fixture-store'}))
    return StorageBackend('local', 'fixture-store', str(root))


def object_path(storage, reference):
    return Path(storage.object_store('').root) / urlsplit(reference.uri).path.lstrip('/')


def test_constructor_is_lazy_and_invalid_fence_prevents_backend_access():
    calls = []
    backend = StorageBackend('aws', 'fixture-store', 'fixture-state', aws_client_factory=lambda _: calls.append(1))
    archive = ArtifactArchive(backend, 'org-one', check_current=lambda: 1)
    assert calls == []
    with pytest.raises(NativeArtifactError):
        archive.retain_bytes('metadata', b'original')
    assert calls == []


def test_original_bytes_are_retained_and_reread_with_content_addressed_uri(storage):
    archive = ArtifactArchive(storage, 'org-one')
    content = b'{ "original": true }\n'
    reference = archive.retain_bytes('collection', content)
    assert reference.uri == ('local://fixture-store/native-collections-v1/'
                             + hashlib.sha256(b'org-one').hexdigest() + '/objects/'
                             + hashlib.sha256(content).hexdigest())
    assert object_path(storage, reference).read_bytes() == content
    assert reference.version == 'sha256:' + reference.sha256
    assert reference.byte_count == len(content)
    assert archive.retain_bytes('second-role', content).uri == reference.uri
    archive.verify()


def test_durable_corruption_is_refused_without_overwriting(storage):
    archive = ArtifactArchive(storage, 'org-one')
    reference = archive.retain_bytes('collection', b'original')
    path = object_path(storage, reference)
    path.write_bytes(b'changed!')
    with pytest.raises(NativeArtifactError):
        archive.verify()
    with pytest.raises(NativeArtifactError):
        archive.retain_bytes('again', b'original')
    assert path.read_bytes() == b'changed!'


def test_retained_content_budget_counts_unique_bytes(storage):
    archive = ArtifactArchive(storage, 'org-one', limits=ArchiveLimits(max_object_bytes=4, max_total_bytes=4))
    archive.retain_bytes('one', b'abcd')
    archive.retain_bytes('two', b'abcd')
    with pytest.raises(NativeArtifactError):
        archive.retain_bytes('three', b'e')


@pytest.mark.parametrize('content', [b'', bytearray(b'not-bytes'), b'12345'])
def test_invalid_or_oversized_objects_do_not_become_artifacts(storage, content):
    archive = ArtifactArchive(storage, 'org-one', limits=ArchiveLimits(max_object_bytes=4))
    with pytest.raises(NativeArtifactError):
        archive.retain_bytes('metadata', content)


def test_file_artifact_is_verified_after_the_source_has_been_removed(storage, tmp_path):
    archive = ArtifactArchive(storage, 'org-one', limits=ArchiveLimits(max_object_bytes=16))
    source = tmp_path / 'source.ndjson'
    content = b'original-framed-stream\n' * 100_000
    source.write_bytes(content)
    reference = archive.retain_file('stream:source', source, expected_sha256=hashlib.sha256(content).hexdigest(),
                                    byte_count=len(content))
    source.unlink()
    assert reference.byte_count == len(content)
    archive.verify()
    object_path(storage, reference).write_bytes(b'truncated')
    with pytest.raises(NativeArtifactError):
        archive.verify()


def test_file_deduplication_never_hides_a_changed_source(storage, tmp_path):
    archive = ArtifactArchive(storage, 'org-one')
    path = tmp_path / 'original'
    path.write_bytes(b'original')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    archive.retain_file('first', path, expected_sha256=expected, byte_count=8)
    path.write_bytes(b'changed!')
    with pytest.raises(NativeArtifactError):
        archive.retain_file('second', path, expected_sha256=expected, byte_count=8)


def test_file_larger_than_metadata_ceiling_uses_streaming_archive(storage, tmp_path):
    archive = ArtifactArchive(storage, 'org-one')
    path = tmp_path / 'large-stream'
    size = ArchiveLimits().max_object_bytes + 1
    with path.open('wb') as target:
        target.seek(size - 1)
        target.write(b'\n')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    reference = archive.retain_file('stream:source', path, expected_sha256=digest, byte_count=size)
    assert object_path(storage, reference).stat().st_size == size
    path.unlink()
    archive.verify()


@pytest.mark.parametrize('change', [{'max_file_bytes': True}, {'max_objects': 0}, {'max_seconds': 7201}])
def test_limits_are_strict_integers(change):
    with pytest.raises(NativeArtifactError):
        replace(ArchiveLimits(), **change)
