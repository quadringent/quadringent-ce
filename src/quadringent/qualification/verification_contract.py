"""Immutable native verification scope; native links and provenance need admission."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, ClassVar, Literal
import unicodedata

from ..capture_binding import CaptureBinding
from ..receiver_identity import BoundCheckpoint, ReceiverIdentity

FORMAT_VERSION = "quadringent-native-verification-scope-v1"
DATASETS = ("source", "snapshot", "journal", "positions", "raw", "history", "mirror")
_KINDS = frozenset({"int", "decimal", "text", "date", "timestamp"})
_TECHNICAL_COLUMNS = frozenset({"event_id", "operation", "journal_receiver", "journal_sequence"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_MAX_TEXT_LENGTH = 256
_MAX_RECEIVER_WINDOWS = 500


def _text(value: object, field: str) -> None:
    if (type(value) is not str or not value or len(value) > _MAX_TEXT_LENGTH
            or value.strip() != value
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)):
        raise ValueError(f"{field} must be bounded nonempty text without controls")


def _identifier(value: object, field: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded unquoted ASCII SQL identifier")


def _record(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("verification contract fields are missing or unknown")
    return value


@dataclass(frozen=True)
class Column:
    name: str
    kind: Literal["int", "decimal", "text", "date", "timestamp"]

    def __post_init__(self) -> None:
        _identifier(self.name, "column name")
        if self.name.casefold() in _TECHNICAL_COLUMNS:
            raise ValueError("business column collides with a technical event field")
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("unsupported verification column kind")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind}

    @classmethod
    def from_dict(cls, record: object) -> Column:
        value = _record(record, {"name", "kind"})
        return cls(value["name"], value["kind"])


@dataclass(frozen=True)
class ReceiverWindow:
    identity: ReceiverIdentity
    start_sequence: int
    end_sequence: int

    def __post_init__(self) -> None:
        if type(self.identity) is not ReceiverIdentity:
            raise ValueError("window requires a native receiver identity")
        if (type(self.start_sequence) is not int or self.start_sequence < 1
                or type(self.end_sequence) is not int or self.end_sequence < self.start_sequence - 1):
            raise ValueError("window requires inclusive integer bounds or an empty interval")

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "start_sequence": self.start_sequence,
                "end_sequence": self.end_sequence}

    @classmethod
    def from_dict(cls, record: object) -> ReceiverWindow:
        value = _record(record, {"identity", "start_sequence", "end_sequence"})
        return cls(ReceiverIdentity.from_dict(value["identity"]), value["start_sequence"], value["end_sequence"])


@dataclass(frozen=True)
class VerificationScope:
    pipeline_id: str
    table_id: str
    copy_run_id: str
    source_library: str
    source_table: str
    destination_database: str
    destination_schema: str
    history_table: str
    mirror_table: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    capture_binding: CaptureBinding
    copy_boundary: BoundCheckpoint
    checkpoint: BoundCheckpoint
    receiver_windows: tuple[ReceiverWindow, ...]
    expected_snapshot_rows: int
    producer_revision: str
    producer_version: str

    FORMAT_VERSION: ClassVar[str] = FORMAT_VERSION

    def __post_init__(self) -> None:
        for name in ("pipeline_id", "table_id", "copy_run_id", "producer_version"):
            _text(getattr(self, name), name)
        for name in ("source_library", "source_table", "destination_database", "destination_schema",
                     "history_table", "mirror_table"):
            _identifier(getattr(self, name), name)
        if self.history_table.casefold() == self.mirror_table.casefold():
            raise ValueError("history and mirror must be distinct tables")
        if type(self.producer_revision) is not str or _REVISION.fullmatch(self.producer_revision) is None:
            raise ValueError("producer revision must be a lowercase SHA40")
        if type(self.expected_snapshot_rows) is not int or self.expected_snapshot_rows < 0:
            raise ValueError("expected snapshot rows must be a non-negative integer")
        if type(self.columns) is not tuple or not self.columns or any(type(c) is not Column for c in self.columns):
            raise ValueError("columns must be a nonempty tuple of Column records")
        names = tuple(column.name for column in self.columns)
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("column names must be unique without case aliases")
        if type(self.primary_key) is not tuple or not self.primary_key:
            raise ValueError("primary key must be a nonempty tuple")
        for key in self.primary_key:
            _identifier(key, "primary key column")
        if len(set(key.casefold() for key in self.primary_key)) != len(self.primary_key):
            raise ValueError("primary key columns must be unique without case aliases")
        if not set(self.primary_key).issubset(names):
            raise ValueError("primary key must use exact declared column names")
        self._validate_bound_windows()

    def _validate_bound_windows(self) -> None:
        if type(self.capture_binding) is not CaptureBinding or self.capture_binding.source_system != "ibmi":
            raise ValueError("native verification requires an IBM i capture binding")
        binding = self.capture_binding
        for name in ("scope_id", "capture_epoch"):
            _text(getattr(binding, name), name)
        if (type(self.receiver_windows) is not tuple or not 1 <= len(self.receiver_windows) <= _MAX_RECEIVER_WINDOWS
                or any(type(window) is not ReceiverWindow for window in self.receiver_windows)):
            raise ValueError("scope requires one to 500 explicit receiver windows")
        claims: dict[str, ReceiverIdentity] = {}

        def admit(identity: ReceiverIdentity) -> None:
            if (type(identity) is not ReceiverIdentity
                    or (identity.journal_library, identity.journal_name) != (
                        binding.bootstrap.receiver_identity.journal_library, binding.journal)):
                raise ValueError("receiver identity belongs to another native journal")
            _text(identity.native_generation_kind, "native generation kind")
            _text(identity.native_generation, "native generation")
            previous = claims.setdefault(identity.receiver_name, identity)
            if previous != identity:
                raise ValueError("receiver name is ambiguous or has been reused")

        for point in (binding.bootstrap, self.copy_boundary, self.checkpoint):
            binding.validate_checkpoint(point)
            _text(point.scope_id, "checkpoint scope")
            _text(point.capture_epoch, "checkpoint epoch")
            if type(point.sequence) is not int or point.sequence < 0:
                raise ValueError("checkpoint sequence must be a non-negative integer")
            admit(point.receiver_identity)
        if (self.copy_boundary.receiver_identity == binding.bootstrap.receiver_identity
                and self.copy_boundary.sequence < binding.bootstrap.sequence):
            raise ValueError("copy boundary precedes the original capture bootstrap")
        seen: set[str] = set()
        for window in self.receiver_windows:
            admit(window.identity)
            if window.identity.receiver_name in seen:
                raise ValueError("receiver windows must not repeat a receiver name")
            seen.add(window.identity.receiver_name)
        first, last = self.receiver_windows[0], self.receiver_windows[-1]
        if (first.identity != self.copy_boundary.receiver_identity
                or first.start_sequence != self.copy_boundary.sequence + 1):
            raise ValueError("first window must follow the exact copy boundary")
        if last.identity != self.checkpoint.receiver_identity or last.end_sequence != self.checkpoint.sequence:
            raise ValueError("last window must end at the exact native checkpoint")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "pipeline_id": self.pipeline_id, "table_id": self.table_id, "copy_run_id": self.copy_run_id,
            "source_library": self.source_library, "source_table": self.source_table,
            "destination_database": self.destination_database, "destination_schema": self.destination_schema,
            "history_table": self.history_table, "mirror_table": self.mirror_table,
            "columns": [column.to_dict() for column in self.columns], "primary_key": list(self.primary_key),
            "capture_binding": self.capture_binding.to_dict(), "copy_boundary": self.copy_boundary.to_dict(),
            "checkpoint": self.checkpoint.to_dict(), "receiver_windows": [w.to_dict() for w in self.receiver_windows],
            "expected_snapshot_rows": self.expected_snapshot_rows,
            "producer_revision": self.producer_revision, "producer_version": self.producer_version,
        }

    @classmethod
    def from_dict(cls, record: object) -> VerificationScope:
        fields = {"format_version", "pipeline_id", "table_id", "copy_run_id", "source_library", "source_table",
                  "destination_database", "destination_schema", "history_table", "mirror_table", "columns",
                  "primary_key", "capture_binding", "copy_boundary", "checkpoint", "receiver_windows",
                  "expected_snapshot_rows", "producer_revision", "producer_version"}
        value = _record(record, fields)
        if type(value["format_version"]) is not str or value["format_version"] != cls.FORMAT_VERSION:
            raise ValueError("unknown native verification scope format")
        if any(type(value[name]) is not list for name in ("columns", "primary_key", "receiver_windows")):
            raise ValueError("serialized scope collections must be JSON arrays")
        if not 1 <= len(value["receiver_windows"]) <= _MAX_RECEIVER_WINDOWS:
            raise ValueError("scope requires one to 500 explicit receiver windows")
        return cls(
            **{name: value[name] for name in fields - {"format_version", "columns", "primary_key", "capture_binding",
                                                     "copy_boundary", "checkpoint", "receiver_windows"}},
            columns=tuple(Column.from_dict(column) for column in value["columns"]),
            primary_key=tuple(value["primary_key"]), capture_binding=CaptureBinding.from_dict(value["capture_binding"]),
            copy_boundary=BoundCheckpoint.from_dict(value["copy_boundary"]),
            checkpoint=BoundCheckpoint.from_dict(value["checkpoint"]),
            receiver_windows=tuple(ReceiverWindow.from_dict(window) for window in value["receiver_windows"]),
        )

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
