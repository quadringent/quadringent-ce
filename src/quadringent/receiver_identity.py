from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, ClassVar

from .contract import JournalPosition


RECEIVER_IDENTITY_FORMAT_VERSION = "as400-receiver-identity-v1"
BOUND_CHECKPOINT_FORMAT_VERSION = "as400-checkpoint-v2"
_SYSTEM_NAME = re.compile(r"[A-Z0-9_$#@]{1,10}")


def _strict_string(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace or controls")


def _strict_record(record: object, fields: set[str], version: str) -> dict[str, Any]:
    if (
        type(record) is not dict
        or set(record) != fields
        or type(record.get("format_version")) is not str
        or record["format_version"] != version
    ):
        raise ValueError("checkpoint identity record format is invalid or unknown")
    return record


@dataclass(frozen=True)
class ReceiverIdentity:
    """An observed native generation; callers must never derive it from names or bounds."""

    journal_library: str
    journal_name: str
    receiver_library: str
    receiver_name: str
    native_generation_kind: str
    native_generation: str

    FORMAT_VERSION: ClassVar[str] = RECEIVER_IDENTITY_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("journal_library", "journal_name", "receiver_library", "receiver_name"):
            value = getattr(self, name)
            _strict_string(value, name)
            if _SYSTEM_NAME.fullmatch(value) is None:
                raise ValueError(f"{name} must be a canonical uppercase ASCII IBM system name of 1 to 10 characters")
        _strict_string(self.native_generation_kind, "native_generation_kind")
        _strict_string(self.native_generation, "native_generation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "journal_library": self.journal_library,
            "journal_name": self.journal_name,
            "receiver_library": self.receiver_library,
            "receiver_name": self.receiver_name,
            "native_generation_kind": self.native_generation_kind,
            "native_generation": self.native_generation,
        }

    @classmethod
    def from_dict(cls, record: object) -> ReceiverIdentity:
        value = _strict_record(record, {
            "format_version", "journal_library", "journal_name", "receiver_library", "receiver_name",
            "native_generation_kind", "native_generation",
        }, cls.FORMAT_VERSION)
        return cls(**{key: value[key] for key in value if key != "format_version"})


@dataclass(frozen=True)
class BoundCheckpoint:
    """A durable position bound to its capture scope, epoch and native receiver."""

    scope_id: str
    capture_epoch: str
    receiver_identity: ReceiverIdentity
    sequence: int

    FORMAT_VERSION: ClassVar[str] = BOUND_CHECKPOINT_FORMAT_VERSION

    def __post_init__(self) -> None:
        _strict_string(self.scope_id, "scope_id")
        _strict_string(self.capture_epoch, "capture_epoch")
        if type(self.receiver_identity) is not ReceiverIdentity:
            raise ValueError("receiver_identity must be a ReceiverIdentity")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("checkpoint sequence must be a non-negative integer")

    @property
    def position(self) -> JournalPosition:
        return JournalPosition(self.receiver_identity.receiver_name, self.sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "scope_id": self.scope_id,
            "capture_epoch": self.capture_epoch,
            "receiver_identity": self.receiver_identity.to_dict(),
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, record: object) -> BoundCheckpoint:
        value = _strict_record(record, {
            "format_version", "scope_id", "capture_epoch", "receiver_identity", "sequence",
        }, cls.FORMAT_VERSION)
        return cls(value["scope_id"], value["capture_epoch"],
                   ReceiverIdentity.from_dict(value["receiver_identity"]), value["sequence"])
