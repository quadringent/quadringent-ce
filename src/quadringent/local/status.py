"""``quadringent local status``: containers, recorded stages and the capture console snapshot."""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from ..installer.runner import CommandRunner
from . import dockerplan as plan
from .config import LocalConfig, LocalPaths
from .orchestrator import Docker, StartError, load_run_state

MAX_SNAPSHOT_BYTES = 1_048_576
LOG_LINES = 40
STAGE_ORDER = ("probe", "table", "admission", "copy")


def _value(entry: object) -> object:
    """Console fields are either a bare value or {"known": ..., "value": ...}."""
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def read_console(paths: LocalPaths) -> dict[str, Any] | None:
    """A few fields of the capture console snapshot, or None when absent or unreadable."""
    path = paths.console_snapshot
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    run = document.get("run") if isinstance(document.get("run"), dict) else {}
    last_error = run.get("last_error") if isinstance(run.get("last_error"), dict) else None
    lag = document.get("lag") if isinstance(document.get("lag"), dict) else {}
    counters = document.get("counters") if isinstance(document.get("counters"), dict) else {}
    position = document.get("position") if isinstance(document.get("position"), dict) else {}
    return {
        "generated_at": document.get("generated_at"),
        "state": run.get("state"),
        "stopped_because": run.get("stopped_because"),
        "last_error_type": last_error.get("type") if last_error else None,
        "checkpoint": position.get("checkpoint"),
        "lag_sequences": _value(lag.get("current")),
        "events_published": _value(counters.get("events_published")),
        "polls": _value(counters.get("polls")),
        "errors": _value(counters.get("errors")),
    }


def collect(config: LocalConfig, paths: LocalPaths, runner: CommandRunner, *, uid: int, gid: int,
            logs: bool = False) -> dict[str, Any]:
    docker = Docker(runner, config, paths, uid=uid, gid=gid)
    containers: dict[str, str | None] = {}
    docker_error = None
    try:
        docker.ready()
        states = docker.states()
        for role in plan.ROLES_SERVICE:
            containers[role] = states.get(plan.container_name(config.name, role), "absent")
    except StartError as error:
        docker_error = str(error)
    try:
        stages = load_run_state(paths)["stages"]
    except StartError as error:
        stages, docker_error = {}, docker_error or str(error)
    document: dict[str, Any] = {
        "instance": config.name,
        "table": f"{config.table_schema}/{config.table_name}",
        "ibmi_host": config.ibmi.host,
        "destination": f"{config.snowflake.account}/{config.snowflake.database}",
        "data_directory": str(paths.data),
        "containers": containers,
        "stages": {name: name in stages for name in STAGE_ORDER},
        "initial_copy_rows": (stages.get("copy") or {}).get("rows"),
        "capture": read_console(paths),
        "delivery": "not measured: the loader prints its progress in its logs; no count is read back from Snowflake",
        "problem": docker_error,
    }
    if logs and docker_error is None:
        document["logs"] = {}
        for role in plan.ROLES_SERVICE:
            result = runner.run(("docker", "logs", "--tail", str(LOG_LINES), plan.container_name(config.name, role)))
            document["logs"][role] = (result.stdout + result.stderr).splitlines()[-LOG_LINES:] if result.ok else []
    return document


def render(document: dict[str, Any]) -> str:
    lines = [
        f"Instance {document['instance']}: {document['table']} from {document['ibmi_host']} to {document['destination']}",
        f"Data directory: {document['data_directory']}",
    ]
    if document["problem"]:
        lines.append(f"Problem: {document['problem']}")
    for role, state in document["containers"].items():
        lines.append(f"  {role:<8} {state}")
    done = [name for name, flag in document["stages"].items() if flag]
    lines.append("Recorded stages: " + (", ".join(done) if done else "none"))
    if document["initial_copy_rows"] is not None:
        lines.append(f"Initial copy: {document['initial_copy_rows']} rows")
    capture = document["capture"]
    if capture is None:
        lines.append("Capture console: no snapshot yet")
    else:
        lines.append(
            f"Capture console ({capture['generated_at']}): state {capture['state']}, lag {capture['lag_sequences']} "
            f"journal entries, {capture['events_published']} events published, {capture['errors']} errors"
            + (f", last error {capture['last_error_type']}" if capture["last_error_type"] else "")
            + (f", stopped: {capture['stopped_because']}" if capture["stopped_because"] else "")
        )
    lines.append(f"Delivery to Snowflake: {document['delivery']}")
    for role, content in (document.get("logs") or {}).items():
        lines.append(f"--- {role} log (last {LOG_LINES} lines)")
        lines.extend(content)
    return "\n".join(lines) + "\n"
