"""Idempotent-write fallbacks retain a fixed read budget even after a race."""
from contextlib import nullcontext
import hashlib
import io
from unittest.mock import patch

import pytest

from quadringent.gcs_backend import GcsObjectStore
from quadringent.object_store import S3ObjectStore
from test_gcs_backend import FakeBlob, FakeGcsClient, GcsError


EXPECTED = b'same'


def s3_error(code):
    error = RuntimeError('synthetic object response')
    error.response = {'Error': {'Code': str(code)}}
    return error


class Body(io.BytesIO):
    def __init__(self, content, *, fragment=None, read_error=False):
        super().__init__(content)
        self.requests, self.total = [], 0
        self.fragment, self.read_error = fragment, read_error

    def read(self, size=None):
        assert size is not None and size >= 0, 'unbounded collision read'
        self.requests.append(size)
        if self.read_error:
            raise OSError('synthetic body failure')
        result = super().read(min(size, self.fragment) if self.fragment else size)
        self.total += len(result)
        return result


class S3:
    def __init__(self, actual, *, race=False, metadata=None, declared=None, fragment=None, read_error=False):
        self.body = Body(actual, fragment=fragment, read_error=read_error)
        self.race, self.metadata, self.declared = race, metadata or {}, declared
        self.heads, self.writes, self.gets = 0, 0, 0

    def head_object(self, **kwargs):
        self.heads += 1
        if self.race and self.heads == 1:
            raise s3_error(404)
        result = {'Metadata': self.metadata}
        if self.declared is not None:
            result['ContentLength'] = self.declared
        return result

    def put_object(self, **kwargs):
        self.writes += 1
        assert self.race and kwargs['IfNoneMatch'] == '*'
        raise s3_error(412)

    def get_object(self, **kwargs):
        self.gets += 1
        result = {'Body': self.body}
        if self.declared is not None:
            result['ContentLength'] = self.declared
        return result


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('metadata', [{}, {'sha256': 'incorrect'}])
def test_s3_equal_fallback_is_bounded_and_closes_the_body(race, metadata):
    client = S3(EXPECTED, race=race, metadata=metadata)
    assert S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED) is False
    assert client.body.closed and client.gets == 1
    assert client.body.total == len(EXPECTED)
    assert all(0 < size <= len(EXPECTED) + 1 for size in client.body.requests)
    assert client.writes == int(race)


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('actual', [b'same' + b'x' * 100000, b'diff', b'sam'])
def test_s3_different_or_oversized_body_is_refused_with_bounded_read_and_close(race, actual):
    client = S3(actual, race=race)
    with pytest.raises(ValueError):
        S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert client.body.closed and client.body.total <= len(EXPECTED) + 1


def test_s3_partial_reads_cannot_hide_a_suffix():
    client = S3(b'same-extra', fragment=4)
    with pytest.raises(ValueError):
        S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert client.body.closed and client.body.total <= len(EXPECTED) + 1


def test_s3_closes_body_on_transport_error():
    client = S3(EXPECTED, read_error=True)
    with pytest.raises(OSError):
        S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert client.body.closed


@pytest.mark.parametrize('declared', [0, 3, 5, True])
def test_s3_refuses_declared_size_different_from_expected_even_with_matching_hash(declared):
    client = S3(EXPECTED, declared=declared, metadata={'sha256': hashlib.sha256(EXPECTED).hexdigest()})
    with pytest.raises(ValueError):
        S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('actual', [EXPECTED, b'diff', b'same' + b'x' * 100000, b'sam'])
def test_gcs_existing_or_race_fallback_is_generation_bound_and_range_limited(race, actual):
    client = FakeGcsClient()
    key = ('sandbox-bucket', 'original')
    if race:
        def winner():
            client.objects[key] = (actual, {}, 7)
        client.before_write = winner
    else:
        client.objects[key] = (actual, {}, 7)
    original = FakeBlob.download_as_bytes
    reads = []

    def read(blob, **kwargs):
        reads.append(kwargs)
        assert kwargs.get('start') == 0 and kwargs.get('end') == len(EXPECTED), 'unbounded GCS collision read'
        assert kwargs.get('if_generation_match') == 7
        return original(blob, **kwargs)

    with patch.object(FakeBlob, 'download_as_bytes', read):
        store = GcsObjectStore('sandbox-bucket', client=client)
        if actual == EXPECTED:
            assert store.put_once('original', EXPECTED) is False
        else:
            with pytest.raises(ValueError):
                store.put_once('original', EXPECTED)
    assert len(reads) == 1
    assert client.objects[key][0] == actual


def test_gcs_generation_replacement_is_refused_without_unpinned_retry():
    client = FakeGcsClient()
    key = ('sandbox-bucket', 'original')
    client.objects[key] = (EXPECTED, {}, 7)
    original = FakeBlob.download_as_bytes
    reads = []

    def replace(blob, **kwargs):
        reads.append(kwargs)
        client.objects[key] = (EXPECTED, {}, 8)
        return original(blob, **kwargs)

    with patch.object(FakeBlob, 'download_as_bytes', replace):
        with pytest.raises(GcsError) as failure:
            GcsObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert failure.value.code == 412
    assert reads == [{'start': 0, 'end': len(EXPECTED), 'if_generation_match': 7}]


def test_s3_get_size_change_is_refused_and_the_open_body_is_closed():
    client = S3(EXPECTED, declared=len(EXPECTED))
    original = client.get_object

    def response(**kwargs):
        result = original(**kwargs)
        result['ContentLength'] = len(EXPECTED) + 1
        return result

    client.get_object = response
    with pytest.raises(ValueError):
        S3ObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert client.body.closed and client.body.total == 0


@pytest.mark.parametrize('declared', [0, 3, 5, True])
def test_gcs_refuses_declared_size_different_from_expected_even_with_matching_hash(declared):
    client = FakeGcsClient()
    client.objects[('sandbox-bucket', 'original')] = (
        EXPECTED, {'sha256': hashlib.sha256(EXPECTED).hexdigest()}, 7)
    with patch.object(FakeBlob, 'size', declared, create=True):
        with pytest.raises(ValueError):
            GcsObjectStore('sandbox-bucket', client=client).put_once('original', EXPECTED)
    assert client.writes == []


def test_s3_empty_existing_object_still_has_a_one_byte_read_budget():
    client = S3(b'')
    assert S3ObjectStore('sandbox-bucket', client=client).put_once('original', b'') is False
    assert client.body.closed and client.body.requests == [1]


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('actual', [EXPECTED, b'diff', b'same' + b'x' * 100000, b'sam', b''])
@pytest.mark.parametrize('expected', [EXPECTED, b''])
def test_local_existing_and_atomic_create_race_use_bounded_collision_reads(tmp_path, race, actual, expected):
    from pathlib import Path
    from quadringent.object_store import FileObjectStore

    store = FileObjectStore(tmp_path)
    target = tmp_path / 'original'
    if not race:
        target.write_bytes(actual)
    original_open = Path.open
    handles = []

    class ReadHandle:
        def __init__(self, handle):
            self.handle, self.total, self.requests = handle, 0, []

        def read(self, size=None):
            assert size is not None and 0 <= size <= len(expected) + 1, 'unbounded local collision read'
            self.requests.append(size)
            data = self.handle.read(size)
            self.total += len(data)
            return data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

    def open_path(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == target and (args[0] if args else kwargs.get('mode', 'r')) == 'rb':
            wrapped = ReadHandle(handle)
            handles.append(wrapped)
            return wrapped
        return handle

    def winner(source, destination):
        target.write_bytes(actual)
        raise FileExistsError('synthetic create race')

    with patch.object(Path, 'open', open_path):
        with patch('quadringent.object_store.os.link', winner) if race else nullcontext():
            if actual == expected:
                assert store.put_once('original', expected) is False
            else:
                with pytest.raises(ValueError):
                    store.put_once('original', expected)
    assert len(handles) == 1 and handles[0].handle.closed
    assert handles[0].total <= len(expected) + 1
    assert target.read_bytes() == actual
    assert list(tmp_path.iterdir()) == [target]


def test_s3_empty_race_winner_keeps_the_one_byte_budget():
    client = S3(b'', race=True)
    assert S3ObjectStore('sandbox-bucket', client=client).put_once('original', b'') is False
    assert client.body.closed and client.body.requests == [1]


@pytest.mark.parametrize('race', [False, True])
def test_gcs_empty_existing_or_race_winner_uses_native_zero_size_without_a_range(race):
    client = FakeGcsClient()
    key = ('sandbox-bucket', 'original')
    if race:
        def winner():
            client.objects[key] = (b'', {}, 7)
        client.before_write = winner
    else:
        client.objects[key] = (b'', {}, 7)
    with patch.object(FakeBlob, 'size', 0, create=True):
        with patch.object(FakeBlob, 'download_as_bytes', side_effect=AssertionError('empty range must not be sent')):
            assert GcsObjectStore('sandbox-bucket', client=client).put_once('original', b'') is False
