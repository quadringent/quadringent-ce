from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .contract import JournalPosition
from .receiver_identity import BOUND_CHECKPOINT_FORMAT_VERSION, BoundCheckpoint


LEGACY_CHECKPOINT_FORMAT_VERSION = "as400-checkpoint-v1"
_LEGACY_AUDIT_FIELDS = {"anchor_actor", "anchor_at", "anchor_reason"}
_ANCHOR_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("checkpoint record contains a duplicate field")
        record[key] = value
    return record


def _decode_checkpoint_json(payload: bytes | str) -> dict[str, Any]:
    record = json.loads(payload, object_pairs_hook=_unique_json_pairs)
    if type(record) is not dict:
        raise ValueError("checkpoint record is not an object")
    return record


def _parse_checkpoint_record(record: object, *, allow_unversioned: bool = False) -> JournalPosition | BoundCheckpoint:
    if type(record) is not dict:
        raise ValueError("invalid checkpoint record")
    if allow_unversioned and "format_version" not in record:
        record = {"format_version": LEGACY_CHECKPOINT_FORMAT_VERSION, **record}
    version = record.get("format_version")
    if version == BOUND_CHECKPOINT_FORMAT_VERSION:
        return BoundCheckpoint.from_dict(record)
    if (
        type(version) is not str
        or version != LEGACY_CHECKPOINT_FORMAT_VERSION
        or set(record) != {"format_version", "receiver", "sequence"}
    ):
        raise ValueError("checkpoint record format is invalid or unknown")
    if type(record["receiver"]) is not str or not record["receiver"].strip():
        raise ValueError("invalid checkpoint receiver")
    if type(record["sequence"]) is not int or record["sequence"] < 0:
        raise ValueError("invalid checkpoint sequence")
    return JournalPosition(record["receiver"], record["sequence"])


def _checkpoint_position(record: JournalPosition | BoundCheckpoint | None) -> JournalPosition | None:
    return record.position if isinstance(record, BoundCheckpoint) else record


def _bound_record(record: JournalPosition | BoundCheckpoint | None) -> BoundCheckpoint | None:
    if isinstance(record, JournalPosition):
        raise ValueError("legacy checkpoint v1 is unbound; automatic binding is forbidden")
    return record


def _legacy_record(position: JournalPosition) -> dict[str, Any]:
    record = {"format_version": LEGACY_CHECKPOINT_FORMAT_VERSION,
              "receiver": position.receiver, "sequence": position.sequence}
    _parse_checkpoint_record(record)
    return record


def _validate_bound_update(previous: BoundCheckpoint | None, candidate: BoundCheckpoint) -> None:
    if type(candidate) is not BoundCheckpoint or (previous is not None and type(previous) is not BoundCheckpoint):
        raise ValueError("bound checkpoint CAS requires BoundCheckpoint envelopes")
    if previous is None:
        return
    old_identity, new_identity = previous.receiver_identity, candidate.receiver_identity
    if (
        previous.scope_id != candidate.scope_id
        or previous.capture_epoch != candidate.capture_epoch
        or old_identity.journal_library != new_identity.journal_library
        or old_identity.journal_name != new_identity.journal_name
    ):
        raise ValueError("bound checkpoint scope, epoch and journal identity cannot change")
    if old_identity.receiver_name == new_identity.receiver_name:
        if old_identity != new_identity:
            raise ValueError("bound checkpoint receiver identity cannot change under the same name")
        if candidate.sequence < previous.sequence:
            raise ValueError("checkpoint moved backwards")


def _checkpoint_digest(record: BoundCheckpoint) -> str:
    payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class JsonCheckpointStore:
    """Durable single-receiver checkpoint used by the local POC.

    The production implementation can replace the file with a transactional
    DynamoDB/S3 or Kafka Connect offset store. The ordering rule remains the
    same: the caller commits here only after the raw batch is durable.
    """

    path: Path

    def _read_checkpoint(self) -> JournalPosition | BoundCheckpoint | None:
        if not self.path.exists():
            return None
        return _parse_checkpoint_record(_decode_checkpoint_json(self.path.read_bytes()))

    def load(self) -> JournalPosition | None:
        return _checkpoint_position(self._read_checkpoint())

    def load_bound(self) -> BoundCheckpoint | None:
        return _bound_record(self._read_checkpoint())

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self.compare_and_set(previous, position)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """Commit a receiver change only when its exact predecessor is known."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self.compare_and_set(previous, position)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        """Serialize local checkpoint writers and preserve the exact predecessor."""
        import fcntl

        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError('checkpoint moved backwards')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self._read_checkpoint()
                if isinstance(current, BoundCheckpoint):
                    raise ValueError("legacy checkpoint write cannot overwrite bound checkpoint v2")
                if current != previous:
                    raise RuntimeError('checkpoint compare-and-set conflict')
                self._write(position)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def compare_and_set_bound(self, previous: BoundCheckpoint | None, candidate: BoundCheckpoint) -> None:
        import fcntl

        _validate_bound_update(previous, candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if _bound_record(self._read_checkpoint()) != previous:
                    raise RuntimeError("checkpoint compare-and-set conflict")
                self._write_record(candidate.to_dict())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, position: JournalPosition) -> None:
        self._write_record(_legacy_record(position))

    def _write_record(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
            directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class DynamoDbCheckpointStore:
    """Transactional single-stream checkpoint backed by one DynamoDB item.

    Bound writes compare the complete envelope; legacy writes cannot replace
    a bound item. Receiver rotation remains an explicit application-level
    decision. The AWS client is lazy so offline tests do not require boto3.
    The default partition key matches the native installer's ``pk``
    schema. Existing custom tables using ``stream_id`` can select that key
    explicitly; the store never probes or migrates their schema.
    """

    FORMAT_VERSION = LEGACY_CHECKPOINT_FORMAT_VERSION

    def __init__(self, table_name: str, stream_key: str, *, client: Any | None = None, partition_key: str = "pk") -> None:
        if not table_name.strip() or table_name.startswith("-"):
            raise ValueError("invalid DynamoDB table name")
        if not stream_key.strip():
            raise ValueError("checkpoint stream key must not be empty")
        if partition_key not in {"pk", "stream_id"}:
            raise ValueError("unsupported DynamoDB checkpoint partition key")
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self.partition_key = partition_key
        self.table_name = table_name
        self.stream_key = stream_key
        self.client = client

    def _read_checkpoint(self) -> JournalPosition | BoundCheckpoint | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={self.partition_key: {"S": self.stream_key}},
            ConsistentRead=True,
        )
        if "Item" not in response:
            return None
        item = response["Item"]
        try:
            if type(item) is not dict or _dynamo_string(item[self.partition_key]) != self.stream_key:
                raise ValueError("checkpoint partition key does not match")
            version = _dynamo_string(item["format_version"]) if "format_version" in item else None
            sequence_attribute = item["sequence"]
            if (
                type(sequence_attribute) is not dict
                or set(sequence_attribute) != {"N"}
                or type(sequence_attribute["N"]) is not str
                or re.fullmatch(r"0|[1-9][0-9]*", sequence_attribute["N"]) is None
            ):
                raise ValueError("invalid checkpoint sequence")
            sequence = int(sequence_attribute["N"])
            receiver = _dynamo_string(item["receiver"])
            if version == BOUND_CHECKPOINT_FORMAT_VERSION:
                if set(item) != {self.partition_key, "format_version", "scope_id", "capture_epoch",
                                 "receiver_identity", "receiver", "sequence", "checkpoint_digest"}:
                    raise ValueError("invalid bound checkpoint attributes")
                identity_attribute = item["receiver_identity"]
                if (
                    type(identity_attribute) is not dict
                    or set(identity_attribute) != {"M"}
                    or type(identity_attribute["M"]) is not dict
                ):
                    raise ValueError("invalid bound receiver identity")
                identity = {name: _dynamo_string(value) for name, value in identity_attribute["M"].items()}
                bound = BoundCheckpoint.from_dict({"format_version": version,
                    "scope_id": _dynamo_string(item["scope_id"]),
                    "capture_epoch": _dynamo_string(item["capture_epoch"]),
                    "receiver_identity": identity, "sequence": sequence})
                if receiver != bound.position.receiver or _dynamo_string(item["checkpoint_digest"]) != _checkpoint_digest(bound):
                    raise ValueError("bound checkpoint digest or receiver does not match")
                return bound
            allowed = {self.partition_key, "receiver", "sequence"}
            if "format_version" in item:
                allowed.add("format_version")
            allowed.update(_LEGACY_AUDIT_FIELDS.intersection(item))
            if set(item) != allowed:
                raise ValueError("invalid legacy checkpoint attributes")
            _validate_legacy_audit(item, version)
            record = {"receiver": receiver, "sequence": sequence}
            if "format_version" in item:
                record["format_version"] = version
            return _parse_checkpoint_record(record, allow_unversioned=True)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid DynamoDB checkpoint item") from error

    def load(self) -> JournalPosition | None:
        return _checkpoint_position(self._read_checkpoint())

    def load_bound(self) -> BoundCheckpoint | None:
        return _bound_record(self._read_checkpoint())

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self._put(position, expected=previous)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """CAS a receiver change against the exact predecessor position."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self._put(position, expected=previous)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        """Do not reload and silently replace the caller's expected predecessor."""
        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError('checkpoint moved backwards')
        self._put(position, expected=previous)

    def _put(self, position: JournalPosition, *, expected: JournalPosition | None) -> None:
        # Reject corrupt stored envelopes before writing; the atomic condition
        # also blocks a bound item installed after this read.
        if isinstance(self._read_checkpoint(), BoundCheckpoint):
            raise ValueError("legacy checkpoint write cannot overwrite bound checkpoint v2")
        _legacy_record(position)
        item = {
            self.partition_key: {"S": self.stream_key},
            "format_version": {"S": self.FORMAT_VERSION},
            "receiver": {"S": position.receiver},
            "sequence": {"N": str(position.sequence)},
        }
        if expected is None:
            condition_expression = "attribute_not_exists(#stream)"
            expression_names = {"#stream": self.partition_key}
            expression_values: dict[str, dict[str, str]] = {}
        else:
            condition_expression = (
                "#receiver = :receiver AND #sequence = :sequence "
                "AND (attribute_not_exists(#format) OR #format = :legacy_format) "
                "AND attribute_not_exists(#scope) AND attribute_not_exists(#epoch) "
                "AND attribute_not_exists(#identity) AND attribute_not_exists(#digest)"
            )
            expression_names = {
                "#receiver": "receiver",
                "#sequence": "sequence",
                "#format": "format_version",
                "#scope": "scope_id",
                "#epoch": "capture_epoch",
                "#identity": "receiver_identity",
                "#digest": "checkpoint_digest",
            }
            expression_values = {
                ":receiver": {"S": expected.receiver},
                ":sequence": {"N": str(expected.sequence)},
                ":legacy_format": {"S": self.FORMAT_VERSION},
            }

        put_arguments: dict[str, Any] = {
            "TableName": self.table_name,
            "Item": item,
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": expression_names,
        }
        if expression_values:
            put_arguments["ExpressionAttributeValues"] = expression_values
        try:
            self.client.put_item(**put_arguments)
        except Exception as error:
            if _is_conditional_failure(error):
                raise RuntimeError("checkpoint compare-and-set conflict") from error
            raise

    def compare_and_set_bound(self, previous: BoundCheckpoint | None, candidate: BoundCheckpoint) -> None:
        _validate_bound_update(previous, candidate)
        if _bound_record(self._read_checkpoint()) != previous:
            raise RuntimeError("checkpoint compare-and-set conflict")
        identity = {name: {"S": value} for name, value in candidate.receiver_identity.to_dict().items()}
        item = {
            self.partition_key: {"S": self.stream_key},
            "format_version": {"S": BOUND_CHECKPOINT_FORMAT_VERSION},
            "scope_id": {"S": candidate.scope_id},
            "capture_epoch": {"S": candidate.capture_epoch},
            "receiver_identity": {"M": identity},
            "receiver": {"S": candidate.position.receiver},
            "sequence": {"N": str(candidate.sequence)},
            "checkpoint_digest": {"S": _checkpoint_digest(candidate)},
        }
        arguments: dict[str, Any] = {"TableName": self.table_name, "Item": item}
        if previous is None:
            arguments.update(ConditionExpression="attribute_not_exists(#stream)",
                             ExpressionAttributeNames={"#stream": self.partition_key})
        else:
            names = {"#format": "format_version", "#scope": "scope_id", "#epoch": "capture_epoch",
                     "#digest": "checkpoint_digest", "#identity": "receiver_identity",
                     "#receiver": "receiver", "#sequence": "sequence"}
            arguments.update(
                ConditionExpression=("#format = :format AND #scope = :scope AND #epoch = :epoch "
                    "AND #digest = :digest AND #identity = :identity AND #receiver = :receiver AND #sequence = :sequence"),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={
                    ":format": {"S": BOUND_CHECKPOINT_FORMAT_VERSION},
                    ":scope": {"S": previous.scope_id}, ":epoch": {"S": previous.capture_epoch},
                    ":digest": {"S": _checkpoint_digest(previous)},
                    ":identity": {"M": {name: {"S": value} for name, value in previous.receiver_identity.to_dict().items()}},
                    ":receiver": {"S": previous.position.receiver}, ":sequence": {"N": str(previous.sequence)},
                },
            )
        try:
            self.client.put_item(**arguments)
        except Exception as error:
            if _is_conditional_failure(error):
                raise RuntimeError("checkpoint compare-and-set conflict") from error
            raise


def _dynamo_string(attribute: object) -> str:
    if type(attribute) is not dict or set(attribute) != {"S"} or type(attribute["S"]) is not str:
        raise ValueError("invalid DynamoDB string attribute")
    return attribute["S"]


def _validate_legacy_audit(item: dict[str, Any], version: str | None) -> None:
    present = _LEGACY_AUDIT_FIELDS.intersection(item)
    if not present:
        return
    # The operator command writes all three fields in one atomic update.
    if version != LEGACY_CHECKPOINT_FORMAT_VERSION or present != _LEGACY_AUDIT_FIELDS:
        raise ValueError("invalid legacy checkpoint audit metadata")
    actor = _dynamo_string(item["anchor_actor"])
    reason = _dynamo_string(item["anchor_reason"])
    timestamp = _dynamo_string(item["anchor_at"])
    if actor != actor.strip() or _ANCHOR_ACTOR.fullmatch(actor) is None:
        raise ValueError("invalid legacy checkpoint audit actor")
    if not reason or reason != reason.strip() or len(reason) > 200:
        raise ValueError("invalid legacy checkpoint audit reason")
    stamp = datetime.fromisoformat(timestamp)
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0) or stamp.isoformat() != timestamp:
        raise ValueError("invalid legacy checkpoint audit timestamp")


def _is_conditional_failure(error: Exception) -> bool:
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    return code == "ConditionalCheckFailedException"
