"""Restore the complete receiver authority without changing original identities."""
from __future__ import annotations

import hashlib
import json

from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import copy_intent_key

from .capture_binding import CaptureBinding, _AUTHORITY
from .copy_observation import MAX_OBSERVATION_BYTES, validate_authority_observations
from .checkpoint import _decode_checkpoint_json
from .object_store import ObjectStore, _validate_object_key
from .receiver_identity import _strict_string
from .receiver_registry import ReceiverNameRegistry, _budget, _digest


AUTHORITY_BACKUP_FORMAT_VERSION = "quadringent-receiver-authority-backup-v1"
DEFAULT_MAX_KEYS = 10000
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_RECORD_BYTES = 65536


def _suspended(value: object) -> None:
    if value is not True:
        raise ValueError("authority writers must be explicitly suspended")


class ReceiverAuthorityBackup:
    """An operator-attested suspended cut, including every domain and copy intent."""

    def __init__(self, store: ObjectStore, *, authority: str) -> None:
        _strict_string(authority, "authority")
        if _AUTHORITY.fullmatch(authority) is None:
            raise ValueError("backup authority must use the canonical stable namespace")
        self.store = store
        self.authority = authority

    def _keys(self, max_keys: int) -> tuple[str, ...]:
        keys = self.store.list_keys(max_keys)
        if type(keys) is not tuple or len(keys) > max_keys:
            raise ValueError("invalid or oversized authority listing")
        for key in keys:
            _validate_object_key(key)
        if len(set(keys)) != len(keys) or keys != tuple(sorted(keys)):
            raise ValueError("authority listing must be unique and sorted")
        return keys

    def _read(self, keys: tuple[str, ...], max_bytes: int) -> dict[str, bytes]:
        objects = {}
        size = 0
        for key in keys:
            maximum = MAX_OBSERVATION_BYTES if key.startswith('copy-observations-v1/') else _RECORD_BYTES
            content = self.store.get_bounded(key, maximum)
            if type(content) is not bytes or len(content) > maximum:
                raise ValueError("authority object exceeds byte budget")
            size += len(content)
            if size > max_bytes:
                raise ValueError("authority inventory byte budget exceeded")
            objects[key] = content
        return objects

    def _snapshot(self, max_keys: int, max_bytes: int) -> dict[str, bytes]:
        keys = self._keys(max_keys)
        objects = self._read(keys, max_bytes)
        if (self._keys(max_keys) != keys or self._read(keys, max_bytes) != objects
                or self._keys(max_keys) != keys):
            raise ValueError("authority changed during inventory observation")
        return objects

    def _validate_objects(self, objects: dict[str, bytes]) -> None:
        domains: dict[tuple[str, str], tuple[ReceiverNameRegistry, dict[str, bytes]]] = {}
        intents: list[tuple[JournalBoundary, ReceiverNameRegistry]] = []
        for key, content in objects.items():
            _validate_object_key(key)
            maximum = MAX_OBSERVATION_BYTES if key.startswith('copy-observations-v1/') else _RECORD_BYTES
            if len(content) > maximum:
                raise ValueError("authority object exceeds byte budget")
            record = _decode_checkpoint_json(content)
            if key.startswith("receiver-registry-v1/"):
                if record.get("format_version") == CaptureBinding.FORMAT_VERSION:
                    binding = CaptureBinding.from_dict(record)
                    source, journal = binding.source_system, binding.journal
                else:
                    source, journal = record.get("source_system"), record.get("journal")
                registry = ReceiverNameRegistry(self.store, authority=self.authority,
                                                source_system=source, journal=journal)
                registry._validate_object(key, content)
                domain = (source, journal)
                if domain not in domains:
                    domains[domain] = (registry, {})
                domains[domain][1][key] = content
            elif key.startswith("copy-intents-v1/"):
                if (set(record) != {"format_version", "pipeline_id", "table_id", "run_id", "boundary"}
                        or record["format_version"] != "quadringent-copy-intent-v1"):
                    raise ValueError("copy intent format is invalid or unknown")
                for field in ("pipeline_id", "table_id", "run_id"):
                    _strict_string(record[field], field)
                if key != copy_intent_key(record["pipeline_id"], record["table_id"], record["run_id"]):
                    raise ValueError("copy intent key differs from its original identity")
                boundary = JournalBoundary.from_dict(record["boundary"])
                boundary.require_bound()
                binding = boundary.capture_binding
                registry = ReceiverNameRegistry(self.store, authority=self.authority,
                                                source_system=binding.source_system, journal=binding.journal)
                registry._validate_binding(binding)
                intents.append((boundary, registry))
            elif key.startswith('copy-observations-v1/'):
                continue  # Validated with their complete original dependency set below.
            else:
                raise ValueError("unknown object in receiver authority")
        validate_authority_observations(objects)
        for registry, domain_objects in domains.values():
            registry._validate_inventory_objects(domain_objects)
        for boundary, registry in intents:
            binding = boundary.capture_binding
            binding_content = objects.get(registry.binding_key(binding.scope_id))
            if binding_content is None or CaptureBinding.from_dict(_decode_checkpoint_json(binding_content)) != binding:
                raise ValueError("copy intent original capture binding is absent or different")
            for point in (binding.bootstrap, boundary.require_bound()):
                name = point.receiver_identity.receiver_name
                content = objects.get(registry.claim_key(name))
                if content is None:
                    raise ValueError("copy intent original receiver claim is absent")
                claim = _decode_checkpoint_json(content)
                registry._validate_claim(claim, name)
                registry._accept_identity(claim, point.receiver_identity)

    def export_inventory(self, *, writers_suspended: bool, max_keys: int = DEFAULT_MAX_KEYS,
                         max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
        """Observe twice; suspension remains an operator attestation."""
        _suspended(writers_suspended)
        _budget(max_keys)
        _budget(max_bytes)
        objects = self._snapshot(max_keys, max_bytes)
        self._validate_objects(objects)
        record = {"format_version": AUTHORITY_BACKUP_FORMAT_VERSION, "registry_authority": self.authority,
                  "objects": [{"key": key, "sha256": hashlib.sha256(content).hexdigest(),
                               "content": content.decode("utf-8")} for key, content in objects.items()]}
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(payload) > max_bytes:
            raise ValueError("authority inventory byte budget exceeded")
        return payload

    def validate_inventory(self, payload: bytes, *, expected_sha256: str, max_keys: int = DEFAULT_MAX_KEYS,
                           max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, bytes]:
        """Validate all provenance and dependencies without storage access."""
        _budget(max_keys)
        _budget(max_bytes)
        _digest(expected_sha256)
        if type(payload) is not bytes or len(payload) > max_bytes:
            raise ValueError("authority inventory byte budget exceeded")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("authority inventory SHA256 differs")
        record = _decode_checkpoint_json(payload)
        if (set(record) != {"format_version", "registry_authority", "objects"}
                or record["format_version"] != AUTHORITY_BACKUP_FORMAT_VERSION
                or record["registry_authority"] != self.authority or type(record["objects"]) is not list):
            raise ValueError("authority inventory format or authority differs")
        if len(record["objects"]) > max_keys:
            raise ValueError("authority inventory key budget exceeded")
        objects: dict[str, bytes] = {}
        size = 0
        for entry in record["objects"]:
            if (type(entry) is not dict or set(entry) != {"key", "sha256", "content"}
                    or type(entry["content"]) is not str):
                raise ValueError("invalid authority inventory entry")
            key = _validate_object_key(entry["key"])
            if key in objects:
                raise ValueError("duplicate authority inventory key")
            _digest(entry["sha256"])
            content = entry["content"].encode("utf-8")
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("authority inventory object SHA256 differs")
            size += len(content)
            if size > max_bytes:
                raise ValueError("authority inventory byte budget exceeded")
            objects[key] = content
        if list(objects) != sorted(objects):
            raise ValueError("authority inventory keys must be sorted")
        self._validate_objects(objects)
        return objects

    def _preflight(self, objects: dict[str, bytes], max_keys: int, max_bytes: int) -> None:
        existing = self._snapshot(max_keys, max_bytes)
        if any(key not in objects or objects[key] != content for key, content in existing.items()):
            raise ValueError("authority restoration collision or unexpected target object")

    def preflight_import(self, payload: bytes, *, expected_sha256: str, writers_suspended: bool,
                         max_keys: int = DEFAULT_MAX_KEYS, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        """Refuse every known target collision before any object is written."""
        _suspended(writers_suspended)
        objects = self.validate_inventory(payload, expected_sha256=expected_sha256,
                                          max_keys=max_keys, max_bytes=max_bytes)
        self._preflight(objects, max_keys, max_bytes)

    def verify_inventory(self, payload: bytes, *, expected_sha256: str, max_keys: int = DEFAULT_MAX_KEYS,
                         max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        objects = self.validate_inventory(payload, expected_sha256=expected_sha256,
                                          max_keys=max_keys, max_bytes=max_bytes)
        if self._snapshot(max_keys, max_bytes) != objects:
            raise ValueError("authority inventory listing or original bytes differ")

    def import_inventory(self, payload: bytes, *, expected_sha256: str, writers_suspended: bool,
                         max_keys: int = DEFAULT_MAX_KEYS, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        """Restore exact bytes only; partial failures never report success."""
        _suspended(writers_suspended)
        objects = self.validate_inventory(payload, expected_sha256=expected_sha256,
                                          max_keys=max_keys, max_bytes=max_bytes)
        self._preflight(objects, max_keys, max_bytes)
        for key, content in objects.items():
            self.store.put_once(key, content)
        self.verify_inventory(payload, expected_sha256=expected_sha256, max_keys=max_keys, max_bytes=max_bytes)
