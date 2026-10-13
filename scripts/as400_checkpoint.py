#!/usr/bin/env python3
"Inspect or advance a durable journal checkpoint under explicit operator controls."

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.checkpoint import DynamoDbCheckpointStore, _is_conditional_failure  # noqa: E402
from quadringent.contract import JournalPosition  # noqa: E402
from quadringent.site_config import SiteConfigurationError, current as current_site  # noqa: E402

_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
_RECEIVER = re.compile(r"[A-Za-z0-9$#@][A-Za-z0-9$#@._-]{0,63}")


class _AuditFailure(RuntimeError):
    error_code = "audit_failed"


class _AuditConflict(_AuditFailure):
    error_code = "audit_conflict"


def _store(args) -> DynamoDbCheckpointStore:
    site = current_site()
    table = (args.table or site.checkpoint_table).strip()
    stream = (args.stream or os.environ.get("AS400_STREAM_KEY", "")).strip()
    if not stream:
        raise SiteConfigurationError("AS400_STREAM_KEY")
    return DynamoDbCheckpointStore(
        table,
        stream,
        client=_dynamodb_client(site.aws_region),
    )


def _dynamodb_client(region: str):
    import boto3

    return boto3.client("dynamodb", region_name=region)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _position_payload(position: JournalPosition | None) -> dict[str, object]:
    if position is None:
        return {"present": False}
    return {
        "present": True,
        "receiver": position.receiver,
        "sequence": position.sequence,
    }


def _anchor(store: DynamoDbCheckpointStore, args) -> dict[str, object]:
    previous = store.load()
    target = JournalPosition(receiver=args.receiver.strip(), sequence=args.sequence)
    if previous is None:
        # With no predecessor, CAS requires the item to remain absent.
        store.compare_and_set(None, target)
    else:
        store.transition(previous, target)
    stamp = _now().isoformat()
    # Audit only the v1 position just written; a concurrent bound checkpoint
    # must retain its complete envelope without legacy metadata.
    try:
        store.client.update_item(
            TableName=store.table_name,
            Key={store.partition_key: {"S": store.stream_key}},
            UpdateExpression=(
                "SET anchor_actor = :actor, anchor_at = :at, anchor_reason = :reason"
            ),
            ConditionExpression=(
                "#receiver = :receiver AND #sequence = :sequence AND #format = :legacy_format "
                "AND attribute_not_exists(#scope) AND attribute_not_exists(#epoch) "
                "AND attribute_not_exists(#identity) AND attribute_not_exists(#digest)"
            ),
            ExpressionAttributeNames={
                "#receiver": "receiver", "#sequence": "sequence", "#format": "format_version",
                "#scope": "scope_id", "#epoch": "capture_epoch", "#identity": "receiver_identity",
                "#digest": "checkpoint_digest",
            },
            ExpressionAttributeValues={
                ":actor": {"S": args.actor.strip()},
                ":at": {"S": stamp},
                ":reason": {"S": args.reason.strip()},
                ":receiver": {"S": target.receiver},
                ":sequence": {"N": str(target.sequence)},
                ":legacy_format": {"S": store.FORMAT_VERSION},
            },
        )
    except Exception as error:
        if _is_conditional_failure(error):
            raise _AuditConflict("checkpoint audit compare-and-set conflict") from error
        raise _AuditFailure("checkpoint audit failed") from error
    return {
        "action": "anchored",
        "actor": args.actor.strip(),
        "at": stamp,
        "reason": args.reason.strip(),
        "previous": _position_payload(previous),
        "position": _position_payload(target),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", default=None, help="default: declared site's stream key")
    parser.add_argument("--table", default=None, help="default: site's checkpoint table")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("status", help="display the stream's durable checkpoint")
    anchor = command.add_parser("anchor", help="reanchor the cursor (audited CAS)")
    anchor.add_argument("--receiver", required=True, help="live target receiver")
    anchor.add_argument("--sequence", type=int, required=True, help="target sequence (>= 0)")
    anchor.add_argument("--actor", required=True, help="operator identity, recorded")
    anchor.add_argument("--reason", required=True, help="reason for reanchoring, recorded")
    args = parser.parse_args()

    try:
        store = _store(args)
    except SiteConfigurationError:
        print(json.dumps({"event": "checkpoint_error", "error": "site_configuration"}, sort_keys=True))
        return 2

    if args.command == "status":
        print(json.dumps({"event": "checkpoint", "stream": store.stream_key,
                          "state": _position_payload(store.load())}, sort_keys=True))
        return 0

    # anchor
    actor = args.actor.strip()
    reason = args.reason.strip()
    if _ACTOR.fullmatch(actor) is None:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_actor"}, sort_keys=True))
        return 2
    if _RECEIVER.fullmatch(args.receiver.strip()) is None:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_receiver"}, sort_keys=True))
        return 2
    if args.sequence < 0:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_sequence"}, sort_keys=True))
        return 2
    if not reason or len(reason) > 200:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_reason"}, sort_keys=True))
        return 2
    try:
        result = _anchor(store, args)
    except _AuditFailure as error:
        print(json.dumps({"event": "checkpoint_error", "error": error.error_code,
                          "position_transitioned": True}, sort_keys=True))
        return 2
    except ValueError as error:
        print(json.dumps({"event": "checkpoint_error", "error": str(error)}, sort_keys=True))
        return 2
    except RuntimeError:
        print(json.dumps({"event": "checkpoint_error", "error": "cas_conflict"}, sort_keys=True))
        return 2
    print(json.dumps({"event": "checkpoint", "stream": store.stream_key, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
