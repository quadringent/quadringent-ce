#!/usr/bin/env python3
"Inspect or reset the IBM i source connection gate under explicit operator controls."

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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

from quadringent.site_config import SiteConfigurationError, current as current_site  # noqa: E402
from quadringent.source_gate import DynamoDbSourceGate  # noqa: E402

_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")


def _gate(args) -> DynamoDbSourceGate:
    site = current_site()
    host = (args.host or site.ibmi_host).strip()
    user = (args.user or site.ibmi_user).strip()
    table = (args.table or site.checkpoint_table).strip()
    return DynamoDbSourceGate(
        table,
        DynamoDbSourceGate.key_for(host, user),
        client=_dynamodb_client(site.aws_region),
    )


def _dynamodb_client(region: str):
    import boto3

    return boto3.client("dynamodb", region_name=region)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="default: declared site's IBM i host")
    parser.add_argument("--user", default=None, help="default: declared site's IBM i account")
    parser.add_argument("--table", default=None, help="default: site's checkpoint table")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("status", help="display the guard's durable state")
    pause = command.add_parser("pause", help="declare a pause (maintenance window)")
    pause.add_argument("--seconds", type=int, required=True,
                       help="pause duration in seconds (60 to 86400)")
    reset = command.add_parser("reset", help="rearm the guard after source intervention")
    reset.add_argument("--actor", required=True,
                       help="operator identity, recorded in the record")
    args = parser.parse_args()

    try:
        gate = _gate(args)
    except SiteConfigurationError as error:
        print(json.dumps({"event": "source_gate_error", "error": "site_configuration"}, sort_keys=True))
        return 2

    if args.command == "status":
        print(json.dumps({"event": "source_gate", "state": gate.state()}, sort_keys=True))
        return 0

    if args.command == "pause":
        if not 60 <= args.seconds <= 86_400:
            print(json.dumps({"event": "source_gate_error", "error": "pause_seconds_range"}, sort_keys=True))
            return 2
        until = _now() + timedelta(seconds=args.seconds)
        record = gate.pause_until(until=until, actor="operator", now=_now())
        print(json.dumps({"event": "source_gate", "action": "paused", "state": record}, sort_keys=True))
        return 0

    # reset
    actor = args.actor.strip()
    if _ACTOR.fullmatch(actor) is None:
        print(json.dumps({"event": "source_gate_error", "error": "invalid_actor"}, sort_keys=True))
        return 2
    record = gate.reset(actor=actor, now=_now())
    print(json.dumps({"event": "source_gate", "action": "reset", "state": record}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
