from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, ClassVar

from .receiver_identity import BoundCheckpoint, ReceiverIdentity, _strict_record, _strict_string


CAPTURE_BINDING_FORMAT_VERSION = "quadringent-capture-binding-v1"
_AUTHORITY = re.compile(r"(?:s3|gcs|local)://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]/receiver-authority-v1")


@dataclass(frozen=True)
class CaptureBinding:
    """The original bootstrap and durable receiver authority for one capture."""

    scope_id: str
    capture_epoch: str
    source_system: str
    journal: str
    registry_authority: str
    bootstrap: BoundCheckpoint

    FORMAT_VERSION: ClassVar[str] = CAPTURE_BINDING_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("scope_id", "capture_epoch", "source_system", "journal", "registry_authority"):
            _strict_string(getattr(self, name), name)
        if _AUTHORITY.fullmatch(self.registry_authority) is None:
            raise ValueError("registry authority must use the canonical stable namespace")
        if type(self.bootstrap) is not BoundCheckpoint:
            raise ValueError("bootstrap must be a BoundCheckpoint")
        self.validate_checkpoint(self.bootstrap)

    def checkpoint(self, identity: ReceiverIdentity, sequence: int) -> BoundCheckpoint:
        point = BoundCheckpoint(self.scope_id, self.capture_epoch, identity, sequence)
        self.validate_checkpoint(point)
        return point

    def validate_checkpoint(self, point: BoundCheckpoint) -> None:
        if type(point) is not BoundCheckpoint:
            raise ValueError("capture checkpoint must be a BoundCheckpoint")
        if point.scope_id != self.scope_id or point.capture_epoch != self.capture_epoch:
            raise ValueError("capture checkpoint scope or epoch differs from its binding")
        identity = point.receiver_identity
        if (identity.journal_name != self.journal
                or identity.journal_library != self.bootstrap.receiver_identity.journal_library):
            raise ValueError("capture checkpoint journal differs from its original bootstrap")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "scope_id": self.scope_id,
            "capture_epoch": self.capture_epoch,
            "source_system": self.source_system,
            "journal": self.journal,
            "registry_authority": self.registry_authority,
            "bootstrap": self.bootstrap.to_dict(),
        }

    @classmethod
    def from_dict(cls, record: object) -> CaptureBinding:
        value = _strict_record(record, {"format_version", "scope_id", "capture_epoch", "source_system",
                                        "journal", "registry_authority", "bootstrap"}, cls.FORMAT_VERSION)
        return cls(value["scope_id"], value["capture_epoch"], value["source_system"], value["journal"],
                   value["registry_authority"], BoundCheckpoint.from_dict(value["bootstrap"]))
