from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from uuid import uuid4

from .capture_binding import CaptureBinding, _AUTHORITY
from .checkpoint import _decode_checkpoint_json
from .contract import JournalPosition
from .object_store import ObjectStore
from .receiver_identity import BoundCheckpoint, ReceiverIdentity, _SYSTEM_NAME, _strict_string


CLAIM_FORMAT_VERSION = "quadringent-receiver-name-claim-v1"
INVENTORY_FORMAT_VERSION = "quadringent-receiver-registry-inventory-v1"
_RECORD_BYTES = 65536
_INVENTORY_BYTES = 16 * 1024 * 1024
_INVENTORY_KEYS = 10000
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _payload(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("invalid registry SHA256")


def _budget(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid registry inventory budget")


class ReceiverNameRegistry:
    """Immutable name ownership in the historical source/journal EVENT_ID domain."""

    def __init__(self, store: ObjectStore, *, authority: str, source_system: str, journal: str) -> None:
        for name, value in (("authority", authority), ("source_system", source_system), ("journal", journal)):
            _strict_string(value, name)
        if _AUTHORITY.fullmatch(authority) is None:
            raise ValueError("registry authority must use the canonical stable namespace")
        if _SYSTEM_NAME.fullmatch(journal) is None:
            raise ValueError("registry journal must be a canonical IBM system name")
        self.store = store
        self.authority = authority
        self.source_system = source_system
        self.journal = journal
        domain = _payload({"source_system": source_system, "journal": journal})
        self.domain_prefix = "receiver-registry-v1/" + hashlib.sha256(domain).hexdigest()

    def binding_key(self, scope_id: str) -> str:
        _strict_string(scope_id, "scope_id")
        return self.domain_prefix + "/bindings/" + hashlib.sha256(scope_id.encode()).hexdigest() + ".json"

    def claim_key(self, receiver_name: str) -> str:
        _strict_string(receiver_name, "receiver_name")
        if _SYSTEM_NAME.fullmatch(receiver_name) is None:
            raise ValueError("registry receiver must be a canonical IBM system name")
        return self.domain_prefix + "/names/" + receiver_name + ".json"

    def _validate_binding(self, binding: CaptureBinding) -> None:
        if type(binding) is not CaptureBinding:
            raise ValueError("registry requires a CaptureBinding")
        if (binding.registry_authority != self.authority or binding.source_system != self.source_system
                or binding.journal != self.journal):
            raise ValueError("capture binding registry authority or EVENT_ID domain differs")

    def _read(self, key: str) -> dict[str, Any] | None:
        try:
            content = self.store.get_bounded(key, _RECORD_BYTES)
        except FileNotFoundError:
            return None
        return _decode_checkpoint_json(content)

    def _put_and_read(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        content = _payload(record)
        if len(content) > _RECORD_BYTES:
            raise ValueError("registry record exceeds byte budget")
        try:
            self.store.put_once(key, content)
        except ValueError:
            # A concurrent writer may have different provenance for the same owner.
            winner = self._read(key)
            if winner is None:
                raise
            return winner
        winner = self._read(key)
        if winner is None:
            raise ValueError("durable registry record is absent after creation")
        return winner

    def load_binding(self, scope_id: str) -> CaptureBinding | None:
        record = self._read(self.binding_key(scope_id))
        if record is None:
            return None
        binding = CaptureBinding.from_dict(record)
        self._validate_binding(binding)
        if binding.scope_id != scope_id:
            raise ValueError("stored capture binding scope differs")
        return binding

    def create_or_load_binding(self, *, scope_id: str, identity: ReceiverIdentity, sequence: int) -> CaptureBinding:
        self.binding_key(scope_id)
        if type(identity) is not ReceiverIdentity or identity.journal_name != self.journal:
            raise ValueError("bootstrap receiver identity differs from registry journal")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("bootstrap sequence must be a non-negative integer")
        previous = self.load_binding(scope_id)
        if previous is not None:
            self.require(previous, previous.bootstrap)
            return previous
        epoch = str(uuid4())
        point = BoundCheckpoint(scope_id, epoch, identity, sequence)
        candidate = CaptureBinding(scope_id, epoch, self.source_system, self.journal, self.authority, point)
        # Reserve first: a crash can leave a claim, never an unprotected binding.
        self._claim_point(candidate, point)
        winner = CaptureBinding.from_dict(self._put_and_read(self.binding_key(scope_id), candidate.to_dict()))
        self._validate_binding(winner)
        if winner.scope_id != scope_id:
            raise ValueError("stored capture binding scope differs")
        self.require(winner, winner.bootstrap)
        return winner

    def _validate_claim(self, record: dict[str, Any], receiver_name: str) -> None:
        common = {"format_version", "state", "source_system", "journal", "registry_authority", "receiver_name"}
        if record.get("state") == "observed":
            fields = common | {"first_checkpoint", "binding"}
        elif record.get("state") == "legacy-unbound":
            fields = common | {"original_checkpoint", "original_proof_sha256"}
        else:
            raise ValueError("receiver claim state is invalid or unknown")
        if set(record) != fields or record.get("format_version") != CLAIM_FORMAT_VERSION:
            raise ValueError("receiver claim format is invalid or unknown")
        if (record["source_system"] != self.source_system or record["journal"] != self.journal
                or record["registry_authority"] != self.authority or record["receiver_name"] != receiver_name):
            raise ValueError("receiver claim authority, domain or name differs")
        self.claim_key(receiver_name)
        if record["state"] == "observed":
            binding = CaptureBinding.from_dict(record["binding"])
            self._validate_binding(binding)
            point = BoundCheckpoint.from_dict(record["first_checkpoint"])
            binding.validate_checkpoint(point)
            if point.receiver_identity.receiver_name != receiver_name:
                raise ValueError("receiver claim identity differs from its name")
        else:
            point = record["original_checkpoint"]
            if (type(point) is not dict or set(point) != {"receiver", "sequence"}
                    or point["receiver"] != receiver_name or type(point["sequence"]) is not int
                    or point["sequence"] < 0):
                raise ValueError("invalid unbound legacy checkpoint")
            _digest(record["original_proof_sha256"])

    def _read_claim(self, receiver_name: str) -> dict[str, Any] | None:
        record = self._read(self.claim_key(receiver_name))
        if record is not None:
            self._validate_claim(record, receiver_name)
        return record

    def _accept_identity(self, record: dict[str, Any], identity: ReceiverIdentity) -> None:
        if record["state"] == "legacy-unbound":
            raise ValueError("legacy-unbound receiver name cannot acquire a native binding")
        if BoundCheckpoint.from_dict(record["first_checkpoint"]).receiver_identity != identity:
            raise ValueError("receiver name reuse with another native identity is forbidden")

    def _claim_point(self, binding: CaptureBinding, point: BoundCheckpoint) -> None:
        self._validate_binding(binding)
        binding.validate_checkpoint(point)
        name = point.receiver_identity.receiver_name
        existing = self._read_claim(name)
        if existing is None:
            record = {"format_version": CLAIM_FORMAT_VERSION, "state": "observed",
                      "source_system": self.source_system, "journal": self.journal,
                      "registry_authority": self.authority, "receiver_name": name,
                      "first_checkpoint": point.to_dict(), "binding": binding.to_dict()}
            existing = self._put_and_read(self.claim_key(name), record)
            self._validate_claim(existing, name)
        self._accept_identity(existing, point.receiver_identity)

    def _require_point(self, point: BoundCheckpoint) -> None:
        record = self._read_claim(point.receiver_identity.receiver_name)
        if record is None:
            raise ValueError("expected receiver claim is absent")
        self._accept_identity(record, point.receiver_identity)

    def require(self, binding: CaptureBinding, point: BoundCheckpoint) -> None:
        self._validate_binding(binding)
        binding.validate_checkpoint(point)
        stored = self.load_binding(binding.scope_id)
        if stored is None:
            raise ValueError("expected capture binding is absent")
        if stored != binding:
            raise ValueError("capture binding differs from the durable original")
        self._require_point(binding.bootstrap)
        if point != binding.bootstrap:
            self._require_point(point)

    def claim(self, binding: CaptureBinding, point: BoundCheckpoint) -> None:
        self._validate_binding(binding)
        binding.validate_checkpoint(point)
        self.require(binding, binding.bootstrap)
        self._claim_point(binding, point)

    def reserve_legacy(self, point: JournalPosition, *, original_proof_sha256: str) -> None:
        if type(point) is not JournalPosition or type(point.sequence) is not int or point.sequence < 0:
            raise ValueError("legacy reservation requires an original journal position")
        key = self.claim_key(point.receiver)
        _digest(original_proof_sha256)
        existing = self._read_claim(point.receiver)
        if existing is None:
            record = {"format_version": CLAIM_FORMAT_VERSION, "state": "legacy-unbound",
                      "source_system": self.source_system, "journal": self.journal,
                      "registry_authority": self.authority, "receiver_name": point.receiver,
                      "original_checkpoint": {"receiver": point.receiver, "sequence": point.sequence},
                      "original_proof_sha256": original_proof_sha256}
            existing = self._put_and_read(key, record)
            self._validate_claim(existing, point.receiver)
        if existing["state"] != "legacy-unbound":
            raise ValueError("observed receiver claim cannot be replaced by a legacy reservation")

    def _validate_object(self, key: str, content: bytes) -> dict[str, Any]:
        if len(content) > _RECORD_BYTES:
            raise ValueError("registry record exceeds byte budget")
        record = _decode_checkpoint_json(content)
        if record.get("format_version") == CaptureBinding.FORMAT_VERSION:
            binding = CaptureBinding.from_dict(record)
            self._validate_binding(binding)
            if key != self.binding_key(binding.scope_id):
                raise ValueError("inventory binding key differs")
        else:
            name = record.get("receiver_name")
            if key != self.claim_key(name):
                raise ValueError("inventory claim key differs")
            self._validate_claim(record, name)
        return record

    def _validate_inventory_objects(self, objects: dict[str, bytes]) -> None:
        records = {key: self._validate_object(key, content) for key, content in objects.items()}
        for record in records.values():
            if record["format_version"] == CaptureBinding.FORMAT_VERSION:
                binding = CaptureBinding.from_dict(record)
                claim = records.get(self.claim_key(binding.bootstrap.receiver_identity.receiver_name))
                if claim is None:
                    raise ValueError("inventory original receiver claim is absent")
                self._accept_identity(claim, binding.bootstrap.receiver_identity)

    def export_inventory(self, keys: Iterable[str], *, max_keys: int = _INVENTORY_KEYS,
                         max_bytes: int = _INVENTORY_BYTES) -> bytes:
        """Export caller-supplied complete keys from a suspended capture."""
        _budget(max_keys)
        _budget(max_bytes)
        objects: dict[str, bytes] = {}
        size = 0
        for key in keys:
            if type(key) is not str or not key.startswith(self.domain_prefix + "/") or key in objects:
                raise ValueError("invalid or duplicate registry inventory key")
            if len(objects) >= max_keys:
                raise ValueError("registry inventory key budget exceeded")
            content = self.store.get_bounded(key, _RECORD_BYTES)
            size += len(content)
            if size > max_bytes:
                raise ValueError("registry inventory byte budget exceeded")
            objects[key] = content
        self._validate_inventory_objects(objects)
        record = {"format_version": INVENTORY_FORMAT_VERSION, "registry_authority": self.authority,
                  "source_system": self.source_system, "journal": self.journal,
                  "objects": [{"key": key, "sha256": hashlib.sha256(content).hexdigest(),
                               "content": content.decode("utf-8")} for key, content in sorted(objects.items())]}
        result = _payload(record)
        if len(result) > max_bytes:
            raise ValueError("registry inventory byte budget exceeded")
        return result

    def _inventory_objects(self, payload: bytes, expected_sha256: str, max_keys: int,
                           max_bytes: int) -> dict[str, bytes]:
        _budget(max_keys)
        _budget(max_bytes)
        _digest(expected_sha256)
        if type(payload) is not bytes or len(payload) > max_bytes:
            raise ValueError("registry inventory byte budget exceeded")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("registry inventory SHA256 differs")
        record = _decode_checkpoint_json(payload)
        if (set(record) != {"format_version", "registry_authority", "source_system", "journal", "objects"}
                or record["format_version"] != INVENTORY_FORMAT_VERSION
                or record["registry_authority"] != self.authority or record["source_system"] != self.source_system
                or record["journal"] != self.journal or type(record["objects"]) is not list):
            raise ValueError("registry inventory authority, domain or format differs")
        if len(record["objects"]) > max_keys:
            raise ValueError("registry inventory key budget exceeded")
        objects = {}
        for entry in record["objects"]:
            if (type(entry) is not dict or set(entry) != {"key", "sha256", "content"}
                    or type(entry["key"]) is not str or type(entry["content"]) is not str):
                raise ValueError("invalid registry inventory entry")
            key = entry["key"]
            if key in objects:
                raise ValueError("duplicate registry inventory key")
            content = entry["content"].encode("utf-8")
            _digest(entry["sha256"])
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("registry inventory object SHA256 differs")
            objects[key] = content
        self._validate_inventory_objects(objects)
        return objects

    def verify_inventory(self, payload: bytes, *, expected_sha256: str, max_keys: int = _INVENTORY_KEYS,
                         max_bytes: int = _INVENTORY_BYTES) -> None:
        objects = self._inventory_objects(payload, expected_sha256, max_keys, max_bytes)
        for key, content in objects.items():
            try:
                actual = self.store.get_bounded(key, _RECORD_BYTES)
            except FileNotFoundError as error:
                raise ValueError("expected inventory claim or binding is absent") from error
            if actual != content:
                raise ValueError("stored registry inventory object differs")

    def import_inventory(self, payload: bytes, *, expected_sha256: str, max_keys: int = _INVENTORY_KEYS,
                         max_bytes: int = _INVENTORY_BYTES) -> None:
        """Restore exact bytes at the same authority; never rewrite provenance."""
        objects = self._inventory_objects(payload, expected_sha256, max_keys, max_bytes)
        for key, content in objects.items():
            self.store.put_once(key, content)
        self.verify_inventory(payload, expected_sha256=expected_sha256, max_keys=max_keys, max_bytes=max_bytes)
