"""Fail-closed comparison of read-only reference pod snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping


@dataclass(frozen=True)
class PodState:
    name: str
    uid: str
    phase: str
    ready: bool
    restarts: int


def snapshot_pods(payload: Mapping[str, object]) -> dict[str, PodState]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("no reference pods observed")
    states: dict[str, PodState] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("invalid reference pod payload")
        metadata = item.get("metadata")
        status = item.get("status")
        if not isinstance(metadata, Mapping) or not isinstance(status, Mapping):
            raise ValueError("invalid reference pod payload")
        name = str(metadata.get("name") or "").strip()
        uid = str(metadata.get("uid") or "").strip()
        if not name or not uid:
            raise ValueError("reference pod identity is missing")
        conditions = status.get("conditions")
        ready = any(
            isinstance(condition, Mapping)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ) if isinstance(conditions, list) else False
        container_statuses = status.get("containerStatuses")
        restarts = sum(
            int(container.get("restartCount") or 0)
            for container in container_statuses
            if isinstance(container, Mapping)
        ) if isinstance(container_statuses, list) else 0
        states[uid] = PodState(
            name=name,
            uid=uid,
            phase=str(status.get("phase") or "Unknown"),
            ready=ready,
            restarts=restarts,
        )
    return states


def evaluate_reference(
    baseline: Mapping[str, PodState],
    current: Mapping[str, PodState],
) -> list[str]:
    baseline_ids = set(baseline)
    current_ids = set(current)
    if baseline_ids != current_ids:
        missing = sorted(baseline[item].name for item in baseline_ids - current_ids)
        added = sorted(current[item].name for item in current_ids - baseline_ids)
        return [
            "reference pod set changed: "
            f"missing={','.join(missing) or '-'}; added={','.join(added) or '-'}"
        ]

    issues: list[str] = []
    for uid in sorted(baseline_ids, key=lambda item: baseline[item].name):
        before = baseline[uid]
        after = current[uid]
        if after.phase != "Running":
            issues.append(f"reference pod {after.name} phase is {after.phase}")
        elif not after.ready:
            issues.append(f"reference pod {after.name} is not Ready")
        if after.restarts > before.restarts:
            issues.append(
                f"reference pod {after.name} restart count increased "
                f"from {before.restarts} to {after.restarts}"
            )
    return issues


def monitor_pilot(
    *,
    read_reference: Callable[[], Mapping[str, object]],
    read_job_state: Callable[[], str],
    stop_pilot: Callable[[list[str]], None],
    baseline_payload: Mapping[str, object] | None = None,
    interval_seconds: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Observe reference and stop only the pilot if the baseline changes."""

    if interval_seconds < 0:
        raise ValueError("interval_seconds must be non-negative")
    baseline = snapshot_pods(
        baseline_payload if baseline_payload is not None else read_reference()
    )
    checks = 0
    while True:
        state = read_job_state().strip().lower()
        if state in {"complete", "failed", "not_found"}:
            return {"verdict": state.upper(), "checks": checks, "issues": []}
        if state != "running":
            issues = [f"pilot state is not observable: {state or 'empty'}"]
            stop_pilot(issues)
            return {
                "verdict": "STOPPED_PILOT_GUARD",
                "checks": checks,
                "issues": issues,
            }
        checks += 1
        try:
            current = snapshot_pods(read_reference())
            issues = evaluate_reference(baseline, current)
        except Exception as error:
            issues = [f"reference observation failed: {type(error).__name__}"]
        if issues:
            stop_pilot(issues)
            return {
                "verdict": "STOPPED_REFERENCE_GUARD",
                "checks": checks,
                "issues": issues,
            }
        sleep(interval_seconds)
