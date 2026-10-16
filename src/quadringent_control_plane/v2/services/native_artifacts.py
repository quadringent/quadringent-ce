"""Retain content-addressed originals; hashes do not grant native admission."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import time

from quadringent.storage_backend import StorageBackend
from .native_verifications import NativeArtifact


_SHA = re.compile(r'[0-9a-f]{64}')
_SCHEMES = {'aws': 's3', 'gcs': 'gcs', 'local': 'local'}


class NativeArtifactError(ValueError):
    """Archive failure without stored content, credentials or provider messages."""


def require(condition):
    if not condition:
        raise NativeArtifactError('native_artifact_invalid_input')


def storage_prefix(storage, org_id, namespace):
    require(type(storage) is StorageBackend and storage.kind in _SCHEMES)
    require(type(storage.raw_bucket) is str
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,222}', storage.raw_bucket) is not None)
    require(type(org_id) is str and 1 <= len(org_id) <= 256 and org_id.strip() == org_id
            and not any(ord(c) < 32 or ord(c) == 127 for c in org_id))
    require(namespace in {'native-preparations-v1', 'native-collections-v1'})
    key = namespace + '/' + hashlib.sha256(org_id.encode()).hexdigest() + '/objects/'
    return _SCHEMES[storage.kind] + '://' + storage.raw_bucket + '/', key


def artifact_key(reference, uri, prefix):
    require(type(reference) is NativeArtifact)
    require(_SHA.fullmatch(reference.sha256) is not None
            and reference.version == 'sha256:' + reference.sha256
            and reference.uri == uri + prefix + reference.sha256)
    return prefix + reference.sha256


@dataclass(frozen=True)
class ArchiveLimits:
    """Bound unique retained bytes; SDK retries and network timeouts are separate."""

    max_object_bytes: int = 32 * 1024**2
    max_file_bytes: int = 2 * 1024**3
    max_total_bytes: int = 16 * 1024**3
    max_objects: int = 100_000
    max_seconds: int = 1800

    def __post_init__(self):
        for value, cap in ((self.max_object_bytes, 64 * 1024**2), (self.max_file_bytes, 16 * 1024**3),
                           (self.max_total_bytes, 128 * 1024**3), (self.max_objects, 100_000),
                           (self.max_seconds, 7200)):
            require(type(value) is int and 1 <= value <= cap)


class ArtifactArchive:
    def __init__(self, storage, org_id, *, check_current=lambda: True, limits=ArchiveLimits()):
        require(type(limits) is ArchiveLimits and callable(check_current))
        self.uri, self.prefix = storage_prefix(storage, org_id, 'native-collections-v1')
        self.storage, self.org_id, self.limits = storage, org_id, limits
        self.check_current = check_current
        self.started = self.previous = time.monotonic()
        self._store = None
        self._records = {}
        self._retained_bytes = 0

    def _current(self):
        now = time.monotonic()
        require(type(now) in (int, float) and math.isfinite(now) and now >= self.previous
                and now - self.started < self.limits.max_seconds and self.check_current() is True)
        self.previous = now
        return True

    def _backend(self):
        self._current()
        if self._store is None:
            self._store = self.storage.object_store('')
        self._current()
        return self._store

    def _candidate(self, name, digest, count):
        self._current()
        require(type(digest) is str and _SHA.fullmatch(digest) is not None
                and type(count) is int and count > 0)
        previous = self._records.get(digest)
        if previous is None:
            require(len(self._records) < self.limits.max_objects
                    and self._retained_bytes + count <= self.limits.max_total_bytes)
        else:
            require(previous[0].byte_count == count)
        return NativeArtifact(name, self.uri + self.prefix + digest, digest, 'sha256:' + digest, count)

    def _remember(self, reference, is_file):
        if reference.sha256 not in self._records:
            self._retained_bytes += reference.byte_count
        previous = self._records.get(reference.sha256)
        self._records[reference.sha256] = (reference, is_file or bool(previous and previous[1]))

    def _read_bytes(self, reference):
        self._current()
        raw = self._backend().get_bounded(artifact_key(reference, self.uri, self.prefix), reference.byte_count)
        self._current()
        require(type(raw) is bytes and len(raw) == reference.byte_count
                and hashlib.sha256(raw).hexdigest() == reference.sha256)
        return raw

    def retain_bytes(self, name, content):
        try:
            require(type(content) is bytes and 1 <= len(content) <= self.limits.max_object_bytes)
            reference = self._candidate(name, hashlib.sha256(content).hexdigest(), len(content))
            self._backend().put_once(self.prefix + reference.sha256, content)
            self._current()
            require(self._read_bytes(reference) == content)
            self._remember(reference, False)
            return reference
        except Exception:
            raise NativeArtifactError('native_artifact_archive_failed') from None

    def retain_file(self, name, path, *, expected_sha256, byte_count):
        try:
            require(type(byte_count) is int and 1 <= byte_count <= self.limits.max_file_bytes)
            reference = self._candidate(name, expected_sha256, byte_count)
            self._backend().put_file_once(self.prefix + reference.sha256, path,
                                         expected_sha256=expected_sha256, byte_count=byte_count,
                                         check_current=self._current)
            self._current()
            self._remember(reference, True)
            return reference
        except Exception:
            raise NativeArtifactError('native_artifact_archive_failed') from None

    def verify(self):
        try:
            self._current()
            for reference, is_file in tuple(self._records.values()):
                if is_file:
                    self._backend().verify_file(artifact_key(reference, self.uri, self.prefix),
                                                expected_sha256=reference.sha256,
                                                byte_count=reference.byte_count, check_current=self._current)
                else:
                    self._read_bytes(reference)
                self._current()
        except Exception:
            raise NativeArtifactError('native_artifact_archive_failed') from None
