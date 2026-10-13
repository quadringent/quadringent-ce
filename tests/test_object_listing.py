from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quadringent.gcs_backend import GcsObjectStore
from quadringent.object_store import FileObjectStore, S3ObjectStore
from test_gcs_backend import FakeGcsClient
from test_receiver_registry import AtomicS3Client


class ListingS3Client(AtomicS3Client):
    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        names = sorted(name for bucket, name in self.objects if bucket == Bucket and name.startswith(Prefix))
        offset = int(ContinuationToken or 0)
        selected = names[offset:offset + min(2, MaxKeys)]
        more = offset + len(selected) < len(names)
        return {"Contents": [{"Key": name} for name in selected], "KeyCount": len(selected),
                "IsTruncated": more, **({"NextContinuationToken": str(offset + len(selected))} if more else {})}


class ListingGcsClient(FakeGcsClient):
    def list_blobs(self, bucket_name, *, prefix, page_size=None, page_token=None, max_results=None):
        names = sorted(name for bucket, name in self.objects if bucket == bucket_name and name.startswith(prefix))
        offset = int(page_token or 0)
        selected = names[offset:offset + min(2, page_size or 2)]
        blobs = [SimpleNamespace(name=name) for name in selected]
        token = str(offset + len(selected)) if offset + len(selected) < len(names) else None
        return SimpleNamespace(pages=iter([iter(blobs)]), next_page_token=token)


@pytest.fixture(params=["file", "s3", "gcs"])
def listing_store(request, tmp_path):
    if request.param == "file":
        return FileObjectStore(tmp_path / "authority")
    if request.param == "s3":
        return S3ObjectStore("registry-bucket", "receiver-authority-v1", client=ListingS3Client())
    return GcsObjectStore("registry-bucket", "receiver-authority-v1", client=ListingGcsClient())


def test_listing_is_exhaustive_relative_sorted_and_budgeted(listing_store):
    keys = ("copy-intents-v1/c.json", "receiver-registry-v1/a/names/OLD.json",
            "receiver-registry-v1/b/bindings/original.json", "receiver-registry-v1/a/names/ORPHAN.json")
    assert listing_store.list_keys(1) == ()
    for key in reversed(keys):
        listing_store.put_once(key, b"original")
    assert listing_store.list_keys(len(keys)) == tuple(sorted(keys))
    with pytest.raises(ValueError, match="budget"):
        listing_store.list_keys(len(keys) - 1)


@pytest.mark.parametrize("budget", [True, False, 0, -1, "2", None])
def test_listing_requires_a_strict_positive_budget(listing_store, budget):
    with pytest.raises(ValueError):
        listing_store.list_keys(budget)


@pytest.mark.parametrize("link_kind", ["file", "directory", "dangling", "root"])
def test_file_listing_refuses_symlinks_without_following_them(tmp_path, link_kind):
    store = FileObjectStore(tmp_path / "root")
    target = tmp_path / "outside"
    target.mkdir()
    (target / "hidden.json").write_bytes(b"outside")
    if link_kind == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        store = FileObjectStore(alias)
    else:
        destination = target / "missing" if link_kind == "dangling" else target / "hidden.json"
        if link_kind == "directory":
            destination = target
        (store.root / "linked").symlink_to(destination, target_is_directory=link_kind == "directory")
    with pytest.raises(ValueError, match="symlink"):
        store.list_keys(10)


@pytest.mark.parametrize("key", ["/absolute", "a//b", "a/./b", "a/../b", "a\\b", "a/", "a/ b", "a/\nkey"])
@pytest.mark.parametrize("backend", ["s3", "gcs"])
def test_remote_listing_rejects_noncanonical_relative_keys(key, backend):
    full_key = "receiver-authority-v1/" + key
    if backend == "s3":
        client = Mock()
        client.list_objects_v2.return_value = {"Contents": [{"Key": full_key}], "IsTruncated": False}
        store = S3ObjectStore("registry-bucket", "receiver-authority-v1", client=client)
    else:
        client = Mock()
        client.list_blobs.return_value = SimpleNamespace(pages=iter([[SimpleNamespace(name=full_key)]]),
                                                        next_page_token=None)
        store = GcsObjectStore("registry-bucket", "receiver-authority-v1", client=client)
    with pytest.raises(ValueError):
        store.list_keys(10)


@pytest.mark.parametrize("page", [{"Contents": [], "IsTruncated": True},
                                  {"Contents": [{"Key": "outside/a"}], "IsTruncated": False},
                                  {"Contents": [{"Key": "receiver-authority-v1/a"}]},
                                  {"Contents": [{"Key": "receiver-authority-v1/a"}], "IsTruncated": "false"},
                                  {"Contents": [{"Key": "receiver-authority-v1/a"}], "KeyCount": 0, "IsTruncated": False}])
def test_s3_listing_refuses_incomplete_or_escaped_pages(page):
    client = Mock()
    client.list_objects_v2.return_value = page
    with pytest.raises(ValueError):
        S3ObjectStore("registry-bucket", "receiver-authority-v1", client=client).list_keys(10)


@pytest.mark.parametrize("backend", ["s3", "gcs"])
def test_listing_refuses_duplicate_keys_and_repeated_page_tokens(backend):
    client = Mock()
    if backend == "s3":
        client.list_objects_v2.return_value = {"Contents": [], "IsTruncated": True,
                                               "NextContinuationToken": "repeated"}
        store = S3ObjectStore("registry-bucket", "receiver-authority-v1", client=client)
    else:
        client.list_blobs.side_effect = lambda *args, **kwargs: SimpleNamespace(pages=iter([[]]),
                                                                             next_page_token="repeated")
        store = GcsObjectStore("registry-bucket", "receiver-authority-v1", client=client)
    with pytest.raises(ValueError, match="pagination"):
        store.list_keys(10)
    if backend == "s3":
        client.list_objects_v2.return_value = {"Contents": [{"Key": "receiver-authority-v1/a"}] * 2,
                                               "IsTruncated": False}
    else:
        client.list_blobs.side_effect = None
        client.list_blobs.return_value = SimpleNamespace(
            pages=iter([[SimpleNamespace(name="receiver-authority-v1/a")] * 2]), next_page_token=None)
    with pytest.raises(ValueError, match="duplicate"):
        store.list_keys(10)
