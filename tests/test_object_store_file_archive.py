"""Complete file archives use bounded streams and conditional publication."""

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path

import pytest

from quadringent.gcs_backend import GcsObjectStore
from quadringent.object_store import FileObjectStore, S3ObjectStore
import quadringent.object_store as object_store

CHUNK = 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def error(code):
    result = RuntimeError("synthetic provider failure")
    result.code = int(code) if str(code).isdigit() else None
    result.response = {"Error": {"Code": str(code)}}
    return result


def consume(stream, size):
    result = bytearray()
    while True:
        part = stream.read(min(CHUNK, size - len(result) + 1))
        assert type(part) is bytes
        if not part:
            return bytes(result)
        result.extend(part)
        assert len(result) <= size


class Body(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.maximum = 0

    def read(self, size=-1):
        assert 0 < size <= CHUNK, "unbounded provider read"
        self.maximum = max(size, self.maximum)
        return super().read(size)


class S3:
    def __init__(self):
        self.objects, self.parts = {}, {}
        self.calls, self.bodies, self.streams = [], [], []
        self.fault = None
        self.hook = lambda event: None

    def event(self, name):
        self.calls.append(name)
        self.hook(name)

    def head_object(self, **kwargs):
        self.event("head")
        key = kwargs["Key"]
        if key not in self.objects:
            raise error(404)
        data = self.objects[key]
        return {"ContentLength": len(data), "ETag": '"' + digest(data) + '"', "Metadata": {"sha256": "untrusted"}}

    def put_object(self, **kwargs):
        self.event("put")
        assert kwargs["IfNoneMatch"] == "*"
        self.streams.append(kwargs["Body"])
        data = consume(kwargs["Body"], kwargs["ContentLength"])
        if kwargs["Key"] in self.objects:
            raise error(412)
        if self.fault == "corrupt":
            data = b"X" + data[1:]
        self.objects[kwargs["Key"]] = data
        return {"ETag": '"' + digest(data) + '"'}

    def create_multipart_upload(self, **kwargs):
        self.event("create")
        self.parts["upload-1"] = []
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.event("part")
        self.streams.append(kwargs["Body"])
        assert kwargs["UploadId"] == "upload-1"
        if self.fault == "part":
            raise error(500)
        data = consume(kwargs["Body"], kwargs["ContentLength"])
        self.parts["upload-1"].append(data)
        return {"ETag": '"' + digest(data) + '"'}

    def complete_multipart_upload(self, **kwargs):
        self.event("complete")
        assert kwargs["IfNoneMatch"] == "*"
        assert [part["PartNumber"] for part in kwargs["MultipartUpload"]["Parts"]] == list(
            range(1, len(self.parts["upload-1"]) + 1)
        )
        if self.fault == "complete409":
            raise error(409)
        if kwargs["Key"] in self.objects:
            raise error(412)
        data = b"".join(self.parts.pop("upload-1"))
        if self.fault == "corrupt":
            data = b"X" + data[1:]
        self.objects[kwargs["Key"]] = data
        return {"ETag": '"' + digest(data) + '"'}

    def abort_multipart_upload(self, **kwargs):
        self.event("abort")
        self.parts.pop(kwargs["UploadId"], None)

    def get_object(self, **kwargs):
        self.event("get")
        data = self.objects[kwargs["Key"]]
        if kwargs.get("IfMatch") != '"' + digest(data) + '"':
            raise error(412)
        body = Body(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data), "ETag": '"' + digest(data) + '"'}


@dataclass
class GcsFailure(Exception):
    code: int


class Gcs:
    def __init__(self):
        self.objects, self.calls, self.streams = {}, [], []
        self.fault = None
        self.hook = lambda event: None
        self.generation = 10

    def event(self, event):
        self.calls.append(event)
        self.hook(event)

    def bucket(self, name):
        return self

    def blob(self, name, **kwargs):
        return Blob(self, name)

    def get_blob(self, name, **kwargs):
        self.event("head")
        return None if name not in self.objects else Blob(self, name)


class Blob:
    def __init__(self, client, name):
        self.client, self.name = client, name
        self.metadata, self.chunk_size = {}, None
        self.generation = client.objects[name][1] if name in client.objects else None
        self.size = len(client.objects[name][0]) if name in client.objects else None

    def upload_from_file(self, stream, *, size, if_generation_match, **kwargs):
        self.client.event("upload")
        assert if_generation_match == 0 and kwargs["retry"] is None
        assert self.chunk_size == CHUNK
        self.client.streams.append(stream)
        data = consume(stream, size)
        if self.name in self.client.objects:
            raise GcsFailure(412)
        if self.client.fault == "corrupt":
            data = b"X" + data[1:]
        self.client.generation += 1
        self.generation, self.size = self.client.generation, len(data)
        self.client.objects[self.name] = (data, self.generation)

    def download_as_bytes(self, *, start, end, if_generation_match, **kwargs):
        self.client.event("range")
        assert kwargs["retry"] is None and kwargs["raw_download"] is True
        assert 0 <= start <= end and end - start + 1 <= CHUNK
        data, generation = self.client.objects[self.name]
        if generation != if_generation_match:
            raise GcsFailure(412)
        return data[start : end + 1]


@pytest.fixture(params=["local", "s3", "gcs"])
def backend(request, tmp_path):
    if request.param == "local":
        return request.param, FileObjectStore(tmp_path / "objects"), None
    client = S3() if request.param == "s3" else Gcs()
    store = (
        S3ObjectStore("sandbox-bucket", client=client)
        if request.param == "s3"
        else GcsObjectStore("sandbox-bucket", client=client)
    )
    return request.param, store, client


def publish(store, source, data, **kwargs):
    return store.put_file_once("original", source, expected_sha256=digest(data), byte_count=len(data), **kwargs)


@pytest.mark.parametrize("data", [b"", b"payload", b"abcd" * (CHUNK // 4 + 7), b"z" * (9 * CHUNK + 1)])
def test_archives_then_replays_the_complete_file_with_bounded_streams(backend, tmp_path, data):
    name, store, client = backend
    source = tmp_path / "source"
    source.write_bytes(data)
    assert publish(store, source, data) is True
    assert publish(store, source, data) is False
    if name == "local":
        assert (store.root / "original").read_bytes() == data
        assert [path.name for path in store.root.iterdir()] == ["original"]
    else:
        archived = client.objects["original"] if name == "s3" else client.objects["original"][0]
        assert archived == data
        assert all(stream.closed for stream in client.streams)
        if name == "s3":
            assert all(body.closed for body in client.bodies)


def test_s3_large_file_is_completed_conditionally_and_fully_read_back(tmp_path):
    data = b"0123456789abcdef" * (6 * CHUNK // 16 + 1)
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3()
    assert publish(S3ObjectStore("sandbox-bucket", client=client), source, data) is True
    assert client.calls.count("part") >= 2 and "complete" in client.calls and "abort" not in client.calls
    assert client.calls[-1] == "get" and client.parts == {}
    assert all(body.closed for body in client.bodies)


@pytest.mark.parametrize("fault", ["part", "complete409"])
def test_s3_incomplete_multipart_is_aborted_without_overwrite_or_unconditional_retry(tmp_path, fault):
    data = b"z" * (6 * CHUNK)
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3()
    client.fault = fault
    with pytest.raises(Exception):
        publish(S3ObjectStore("sandbox-bucket", client=client), source, data)
    assert client.calls.count("abort") == 1 and client.parts == {} and client.objects == {}


@pytest.mark.parametrize("bad", ["hash", "length", "symlink", "directory"])
def test_bad_source_is_refused_before_remote_effects(backend, tmp_path, bad):
    name, store, client = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    expected = data
    if bad == "hash":
        source.write_bytes(b"changed")
    elif bad == "length":
        source.write_bytes(data + b"x")
    elif bad == "symlink":
        link = tmp_path / "link"
        link.symlink_to(source)
        source = link
    else:
        source = tmp_path
    with pytest.raises(ValueError):
        publish(store, source, expected)
    assert client is None or client.calls == []


def test_existing_archive_does_not_hide_a_mutated_source(backend, tmp_path):
    _, store, _ = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    assert publish(store, source, data) is True
    source.write_bytes(b"changed")
    with pytest.raises(ValueError):
        publish(store, source, data)


@pytest.mark.parametrize("stale", [False, 1, None])
def test_fence_must_be_exactly_true_before_any_effect(backend, tmp_path, stale):
    _, store, client = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    with pytest.raises(ValueError):
        publish(store, source, data, check_current=lambda: stale)
    assert client is None or client.calls == []


@pytest.mark.parametrize("name", ["s3", "gcs"])
def test_snapshot_prevents_a_provider_from_uploading_a_later_source_mutation(tmp_path, name):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3() if name == "s3" else Gcs()
    client.hook = lambda event: source.write_bytes(b"mutated") if event in {"put", "upload"} else None
    store = (
        S3ObjectStore("sandbox-bucket", client=client)
        if name == "s3"
        else GcsObjectStore("sandbox-bucket", client=client)
    )
    assert publish(store, source, data) is True
    assert source.read_bytes() == b"mutated"
    assert (client.objects["original"] if name == "s3" else client.objects["original"][0]) == data


@pytest.mark.parametrize("name", ["s3", "gcs"])
def test_full_readback_rejects_provider_corruption(tmp_path, name):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3() if name == "s3" else Gcs()
    client.fault = "corrupt"
    store = (
        S3ObjectStore("sandbox-bucket", client=client)
        if name == "s3"
        else GcsObjectStore("sandbox-bucket", client=client)
    )
    with pytest.raises(ValueError):
        publish(store, source, data)


@pytest.mark.parametrize("name", ["s3", "gcs"])
def test_create_race_compares_all_winner_bytes_and_preserves_the_winner(tmp_path, name):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3() if name == "s3" else Gcs()

    def winner(event):
        if event in {"put", "upload"}:
            client.objects["original"] = b"collision" if name == "s3" else (b"collision", 99)

    client.hook = winner
    store = (
        S3ObjectStore("sandbox-bucket", client=client)
        if name == "s3"
        else GcsObjectStore("sandbox-bucket", client=client)
    )
    with pytest.raises(ValueError):
        publish(store, source, data)
    assert (client.objects["original"] if name == "s3" else client.objects["original"][0]) == b"collision"


@pytest.mark.parametrize("data", [b"", b"archive" * (CHUNK // 7 + 3)])
def test_durable_verification_survives_source_cleanup(backend, tmp_path, data):
    _, store, _ = backend
    source = tmp_path / "source"
    source.write_bytes(data)
    assert publish(store, source, data) is True
    source.unlink()
    assert store.verify_file("original", expected_sha256=digest(data), byte_count=len(data)) is None


@pytest.mark.parametrize("mutation", ["corruption", "truncation", "extension"])
def test_durable_verification_detects_later_mutation(backend, tmp_path, mutation):
    name, store, client = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    assert publish(store, source, data) is True
    source.unlink()
    changed = b"changed" if mutation == "corruption" else data[:-1] if mutation == "truncation" else data + b"x"
    if name == "local":
        (store.root / "original").write_bytes(changed)
    elif name == "s3":
        client.objects["original"] = changed
    else:
        client.objects["original"] = (changed, 99)
    with pytest.raises(ValueError):
        store.verify_file("original", expected_sha256=digest(data), byte_count=len(data))
    if name == "s3":
        assert all(body.closed for body in client.bodies)


def test_durable_verification_refuses_absence_and_stale_context(backend):
    _, store, client = backend
    with pytest.raises((FileNotFoundError, ValueError)):
        store.verify_file("absent", expected_sha256=digest(b""), byte_count=0)
    if client is not None:
        client.calls.clear()
    with pytest.raises(ValueError):
        store.verify_file("absent", expected_sha256=digest(b""), byte_count=0, check_current=lambda: 1)
    assert client is None or client.calls == []


@pytest.mark.parametrize("mutation", ["metadata", "replacement"])
def test_source_metadata_or_identity_change_during_snapshot_is_refused(backend, tmp_path, monkeypatch, mutation):
    _, store, client = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    original_write = object_store._archive_write

    def changed_write(output, part, check):
        original_write(output, part, check)
        if mutation == "replacement":
            source.unlink()
            source.write_bytes(data)
        else:
            metadata = source.stat()
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))

    monkeypatch.setattr(object_store, "_archive_write", changed_write)
    with pytest.raises(ValueError, match="changed during observation"):
        publish(store, source, data)
    assert client is None or client.calls == []


@pytest.mark.parametrize("identical", [True, False])
def test_local_link_race_preserves_and_fully_compares_winner(tmp_path, monkeypatch, identical):
    data = b"payload"
    winner = data if identical else b"changed"
    source = tmp_path / "source"
    source.write_bytes(data)
    store = FileObjectStore(tmp_path / "objects")
    original_link = os.link

    def raced_link(temporary, destination, **arguments):
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=arguments.get("dst_dir_fd")
        )
        with open(descriptor, "wb", closefd=True) as output:
            output.write(winner)
        original_link(temporary, destination, **arguments)

    monkeypatch.setattr(os, "link", raced_link)
    if identical:
        assert publish(store, source, data) is False
    else:
        with pytest.raises(ValueError):
            publish(store, source, data)
    assert (store.root / "original").read_bytes() == winner
    assert [path.name for path in store.root.iterdir()] == ["original"]


@pytest.mark.parametrize("identical", [True, False])
def test_multipart_race_aborts_parts_then_verifies_the_winner(tmp_path, identical):
    data = b"a" * (6 * CHUNK)
    winner = data if identical else b"b" * len(data)
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3()

    def raced_complete(event):
        if event == "complete":
            client.objects["original"] = winner

    client.hook = raced_complete
    store = S3ObjectStore("sandbox-bucket", client=client)
    if identical:
        assert publish(store, source, data) is False
    else:
        with pytest.raises(ValueError):
            publish(store, source, data)
    assert client.calls.count("abort") == 1 and client.parts == {}
    assert client.objects["original"] == winner
    assert all(stream.closed for stream in client.streams)
    assert all(body.closed for body in client.bodies)


@pytest.mark.parametrize("event", ["part", "get"])
def test_stale_s3_context_aborts_incomplete_parts_and_closes_readback(tmp_path, event):
    data = b"a" * (6 * CHUNK)
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3()
    current = True

    def changed_context(name):
        nonlocal current
        if name == event:
            current = False

    client.hook = changed_context
    with pytest.raises(ValueError):
        publish(S3ObjectStore("sandbox-bucket", client=client), source, data, check_current=lambda: current)
    assert client.parts == {}
    assert client.calls.count("abort") == (1 if event == "part" else 0)
    assert all(stream.closed for stream in client.streams)
    assert all(body.closed for body in client.bodies)


def test_gcs_changed_generation_between_readback_ranges_is_never_retried_unpinned(tmp_path):
    data = b"a" * (2 * CHUNK)
    source = tmp_path / "source"
    source.write_bytes(data)
    client = Gcs()
    ranges = 0

    def changed_generation(event):
        nonlocal ranges
        if event == "range":
            ranges += 1
            if ranges == 2:
                client.objects["original"] = (data, 99)

    client.hook = changed_generation
    with pytest.raises(GcsFailure):
        publish(GcsObjectStore("sandbox-bucket", client=client), source, data)
    assert client.calls.count("range") == 2
    assert all(stream.closed for stream in client.streams)


@pytest.mark.parametrize("name,event", [("s3", "get"), ("gcs", "range")])
def test_verify_readback_refuses_deadline_and_closes_body(tmp_path, name, event):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3() if name == "s3" else Gcs()
    store = (
        S3ObjectStore("sandbox-bucket", client=client)
        if name == "s3"
        else GcsObjectStore("sandbox-bucket", client=client)
    )
    assert publish(store, source, data) is True
    timed_out = False

    def changed_context(current):
        nonlocal timed_out
        if current == event:
            timed_out = True

    def check_current():
        if timed_out:
            raise ValueError("synthetic deadline")
        return True

    client.hook = changed_context
    with pytest.raises(ValueError, match="deadline"):
        store.verify_file("original", expected_sha256=digest(data), byte_count=len(data), check_current=check_current)
    if name == "s3":
        assert all(body.closed for body in client.bodies)


def test_parent_symlink_and_nonregular_source_are_refused(backend, tmp_path):
    _, store, client = backend
    directory = tmp_path / "real"
    directory.mkdir()
    (directory / "source").write_bytes(b"payload")
    link = tmp_path / "linked"
    link.symlink_to(directory, target_is_directory=True)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for source in (link / "source", fifo):
        with pytest.raises(ValueError):
            publish(store, source, b"payload")
    assert client is None or client.calls == []


@pytest.mark.parametrize("bad", ["sha", "count", "negative", "boolean", "fence"])
def test_invalid_archive_identity_is_refused_before_provider_access(backend, tmp_path, bad):
    _, store, client = backend
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    arguments = {"expected_sha256": digest(data), "byte_count": len(data), "check_current": lambda: True}
    arguments[
        {
            "sha": "expected_sha256",
            "count": "byte_count",
            "negative": "byte_count",
            "boolean": "byte_count",
            "fence": "check_current",
        }[bad]
    ] = {"sha": "ABCDEF", "count": 7.0, "negative": -1, "boolean": True, "fence": None}[bad]
    with pytest.raises(ValueError):
        store.put_file_once("original", source, **arguments)
    with pytest.raises(ValueError):
        store.verify_file("original", **arguments)
    assert client is None or client.calls == []


def test_s3_pins_available_version_and_rejects_mismatched_readback_version(tmp_path):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    client = S3()
    store = S3ObjectStore("sandbox-bucket", client=client)
    assert publish(store, source, data) is True
    original_head, original_get = client.head_object, client.get_object

    def head(**arguments):
        return {**original_head(**arguments), "VersionId": "generation-one"}

    def wrong_version(**arguments):
        assert arguments["VersionId"] == "generation-one"
        return {**original_get(**arguments), "VersionId": "generation-two"}

    client.head_object, client.get_object = head, wrong_version
    with pytest.raises(ValueError):
        store.verify_file("original", expected_sha256=digest(data), byte_count=len(data))
    assert all(body.closed for body in client.bodies)


def test_upload_reader_requires_bounded_reads_and_supports_sdk_rewind(tmp_path):
    data = b"0123" * (3 * CHUNK)
    source = tmp_path / "source"
    source.write_bytes(data)
    with object_store._archive_snapshot(source, digest(data), len(data), lambda: True) as snapshot:
        reader = object_store._ArchiveReader(snapshot, lambda: True, 0, len(data))
        with pytest.raises(ValueError, match="bounded"):
            reader.read()
        assert reader.read(8 * CHUNK) == data[: 8 * CHUNK]
        assert reader.tell() == 8 * CHUNK
        reader.seek(0)
        assert reader.read(CHUNK) == data[:CHUNK]
        reader.seek(0, os.SEEK_END)
        assert reader.read(CHUNK) == b""
        with pytest.raises(ValueError):
            reader.seek(1, os.SEEK_END)
    assert reader.closed


def test_local_archive_rejects_symlinked_destination_before_any_outside_write(tmp_path):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    store = FileObjectStore(tmp_path / "objects")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "redirected").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.put_file_once("redirected/object", source, expected_sha256=digest(data), byte_count=len(data))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("boundary", ["mkdir", "link"])
def test_local_archive_parent_substitution_never_redirects_a_mutation(tmp_path, monkeypatch, boundary):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    store = FileObjectStore(tmp_path / "objects")
    parent, moved = store.root / "parent", store.root / "moved"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = getattr(os, boundary)

    def substitute(*arguments, **keywords):
        if boundary == "link" or str(arguments[0]).endswith("nested"):
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
        return original(*arguments, **keywords)

    monkeypatch.setattr(os, boundary, substitute)
    key = "parent/nested/object" if boundary == "mkdir" else "parent/object"
    with pytest.raises((OSError, ValueError)):
        store.put_file_once(key, source, expected_sha256=digest(data), byte_count=len(data))
    assert list(outside.iterdir()) == []
    assert not list(store.root.rglob(".archive-*.tmp"))


def test_local_archive_refuses_a_replaced_root_before_destination_effects(tmp_path):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    store = FileObjectStore(tmp_path / "objects")
    store.root.rename(tmp_path / "original-root")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.put_file_once("nested/object", source, expected_sha256=digest(data), byte_count=len(data))
    assert list(outside.iterdir()) == []


def test_local_archive_preserves_a_symlink_collision_and_its_outside_target(tmp_path):
    data = b"payload"
    source = tmp_path / "source"
    source.write_bytes(data)
    store = FileObjectStore(tmp_path / "objects")
    outside = tmp_path / "outside-object"
    outside.write_bytes(data)
    (store.root / "object").symlink_to(outside)
    with pytest.raises(ValueError):
        store.put_file_once("object", source, expected_sha256=digest(data), byte_count=len(data))
    with pytest.raises(ValueError):
        store.verify_file("object", expected_sha256=digest(data), byte_count=len(data))
    assert outside.read_bytes() == data
    assert (store.root / "object").is_symlink()
    assert not list(store.root.rglob(".archive-*.tmp"))
