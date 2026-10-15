"""``quadringent local status``: containers, recorded stages and the capture console snapshot."""

from __future__ import annotations

import json
import os
import secrets
import stat
from typing import Any

from ..installer.runner import CommandRunner
from . import dockerplan as plan
from .config import LocalConfig, LocalPaths
from .discovery import JobOutputError, result_line
from .orchestrator import RESULT_PREFIX, Docker, StartError, failure_reason, load_run_state

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
    return summarize_console(document)


def summarize_console(document: object) -> dict[str, Any] | None:
    """The fields ``status`` shows, from a console snapshot read from a folder or from a Docker volume."""
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


def read_volume(docker: Docker, stages: dict, problems: list[str]) -> tuple[dict[str, Any] | None, int | None]:
    """Console snapshot and copy-evidence row count out of the data volume, through a read-only helper container.

    A missing volume is not created (``docker run`` would create an unlabelled one): it reads as "no snapshot".
    """
    labels = docker.volume_labels()
    if labels is None:
        return None, None
    docker._require_owned_volume(labels)  # a volume that merely has this name is not read
    if not docker.image_present(docker.config.capture_image):
        problems.append(f"the data volume was not read: image {docker.config.capture_image} is not present "
                        "locally (status never pulls an image; start does)")
        return None, None
    key = (stages.get("copy") or {}).get("evidence_key")
    spec = plan.read_status_spec(docker.config, docker.paths, evidence_key=key if isinstance(key, str) else None,
                                 name_suffix=secrets.token_hex(4))
    result = docker.run(spec)
    try:
        document = result_line(result.stdout, RESULT_PREFIX)
    except JobOutputError:
        problems.append(f"the data volume could not be read: {failure_reason(result)}")
        return None, None
    rows = document.get("evidence_rows")
    return summarize_console(document.get("console")), rows if isinstance(rows, int) else None


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
    capture, evidence_rows = None, None
    if config.uses_volume:
        if docker_error is None:
            volume_problems: list[str] = []
            try:
                capture, evidence_rows = read_volume(docker, stages, volume_problems)
            except StartError as error:
                volume_problems.append(str(error))
            docker_error = volume_problems[0] if volume_problems else None
    else:
        capture = read_console(paths)
    document: dict[str, Any] = {
        "instance": config.name,
        "table": f"{config.table_schema}/{config.table_name}",
        "ibmi_host": config.ibmi.host,
        "destination": f"{config.snowflake.account}/{config.snowflake.database}",
        "data_location": config.data_location,
        "data_directory": None if config.uses_volume else str(paths.data),  # always a path, or null for a volume
        "data_volume": config.volume_name if config.uses_volume else None,
        "containers": containers,
        "stages": {name: name in stages for name in STAGE_ORDER},
        "initial_copy_rows": (stages.get("copy") or {}).get("rows"),
        "initial_copy_evidence_rows": evidence_rows,
        "capture": capture,
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
        (f"Data volume: docker volume {document['data_volume']}" if document["data_volume"]
         else f"Data directory: {document['data_directory']}"),
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
