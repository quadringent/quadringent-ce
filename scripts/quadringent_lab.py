#!/usr/bin/env python3
"""Quadringent local laboratory command line.

    python scripts/quadringent_lab.py start [--scenario NAME] [--port 8877]
    python scripts/quadringent_lab.py stop [--port 8877]
    python scripts/quadringent_lab.py status [--port 8877]
    python scripts/quadringent_lab.py list

The laboratory serves the real built frontend (``ui/dist``) and the real v2
control plane over a throwaway SQLite file, bound to 127.0.0.1 only, with
IBM i, Snowflake, Kubernetes and the cloud replaced at the product's
injectable boundaries. Everything it creates lives in one temporary
directory that is removed on exit. See docs/product/local-lab.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quadringent_lab import DEFAULT_PORT  # noqa: E402
from quadringent_lab import server  # noqa: E402
from quadringent_lab.scenarios import DEFAULT_SCENARIO, SCENARIOS, UnknownScenarioError, get_scenario  # noqa: E402


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        scenario = get_scenario(args.scenario)
    except UnknownScenarioError:
        print(f"unknown scenario: {args.scenario}; run `list` to see the catalog", file=sys.stderr)
        return 2
    if args.detach:
        return _start_detached(args, scenario.name)
    ui_dist = Path(args.ui_dist).resolve() if args.ui_dist else None
    return server.serve(
        scenario=scenario.name,
        port=args.port,
        ui_dist=ui_dist,
        allow_shutdown=args.allow_shutdown,
        announce=lambda line: print(line, flush=True),
    )


def _start_detached(args: argparse.Namespace, scenario: str) -> int:
    if args.port == 0:
        print("--detach needs an explicit port", file=sys.stderr)
        return 2
    if server.lab_identity(args.port) is not None:
        print(f"a laboratory already answers on port {args.port}", file=sys.stderr)
        return 1
    server.registry_dir().mkdir(parents=True, exist_ok=True)
    log_path = server.registry_dir() / f"{args.port}.log"
    command = [sys.executable, str(Path(__file__).resolve()), "start", "--scenario", scenario, "--port", str(args.port)]
    if args.ui_dist:
        command += ["--ui-dist", args.ui_dist]
    if args.allow_shutdown:
        command.append("--allow-shutdown")
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"laboratory exited early; see {log_path}", file=sys.stderr)
            return 1
        if server.lab_identity(args.port) is not None:
            print(f"READY http://127.0.0.1:{args.port}/ pid={process.pid} scenario={scenario}")
            return 0
        time.sleep(0.2)
    print(f"laboratory did not answer within 30 s; see {log_path}", file=sys.stderr)
    return 1


def _cmd_stop(args: argparse.Namespace) -> int:
    message = server.stop(args.port)
    print(message)
    return 0 if ("stopped" in message or "removed" in message) else 1


def _cmd_status(args: argparse.Namespace) -> int:
    identity = server.lab_identity(args.port)
    if identity is None:
        print(f"no laboratory on port {args.port}")
        return 1
    print(json.dumps(identity, indent=2))
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    for scenario in SCENARIOS:
        marker = {"backend": "api", "backend+browser": "api+ui", "browser": "ui", "cli": "cli"}[scenario.coverage]
        print(f"{scenario.number:>2}  {scenario.name:<28} [{marker}]  {scenario.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start the laboratory in the foreground (Ctrl+C stops it)")
    start.add_argument("--scenario", default=DEFAULT_SCENARIO, help="scenario name or number (default: baseline)")
    start.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port (0 picks a free one)")
    start.add_argument("--ui-dist", help="built frontend directory (default: ui/dist)")
    start.add_argument("--allow-shutdown", action="store_true", help="enable POST /lab/shutdown (used by browser checks)")
    start.add_argument("--detach", action="store_true", help="run in the background and return once ready")
    start.set_defaults(handler=_cmd_start)

    stop = sub.add_parser("stop", help="stop the laboratory on a port and remove its directory")
    stop.add_argument("--port", type=int, default=DEFAULT_PORT)
    stop.set_defaults(handler=_cmd_stop)

    status = sub.add_parser("status", help="show whether a laboratory answers on a port")
    status.add_argument("--port", type=int, default=DEFAULT_PORT)
    status.set_defaults(handler=_cmd_status)

    listing = sub.add_parser("list", help="list the scenario catalog")
    listing.set_defaults(handler=_cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
