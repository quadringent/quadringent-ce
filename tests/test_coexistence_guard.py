from __future__ import annotations

import unittest

from quadringent.coexistence import evaluate_reference, monitor_pilot, snapshot_pods


def pod_payload(
    *,
    name: str = "source-abc",
    uid: str = "uid-1",
    ready: bool = True,
    phase: str = "Running",
    restarts: int = 91,
) -> dict:
    return {
        "items": [
            {
                "metadata": {"name": name, "uid": uid},
                "status": {
                    "phase": phase,
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                        }
                    ],
                    "containerStatuses": [
                        {"name": "capture", "restartCount": restarts}
                    ],
                },
            }
        ]
    }


class CoexistenceGuardTests(unittest.TestCase):
    def test_unchanged_ready_reference_is_safe(self) -> None:
        baseline = snapshot_pods(pod_payload())

        self.assertEqual(evaluate_reference(baseline, snapshot_pods(pod_payload())), [])

    def test_new_restart_opens_the_guard(self) -> None:
        baseline = snapshot_pods(pod_payload(restarts=91))

        issues = evaluate_reference(
            baseline,
            snapshot_pods(pod_payload(restarts=92)),
        )

        self.assertEqual(
            issues,
            ["reference pod source-abc restart count increased from 91 to 92"],
        )

    def test_unready_pod_opens_the_guard(self) -> None:
        baseline = snapshot_pods(pod_payload())

        issues = evaluate_reference(
            baseline,
            snapshot_pods(pod_payload(ready=False)),
        )

        self.assertEqual(issues, ["reference pod source-abc is not Ready"])

    def test_pod_replacement_opens_the_guard(self) -> None:
        baseline = snapshot_pods(pod_payload(name="source-old", uid="uid-old"))

        issues = evaluate_reference(
            baseline,
            snapshot_pods(pod_payload(name="source-new", uid="uid-new", restarts=0)),
        )

        self.assertEqual(
            issues,
            [
                "reference pod set changed: missing=source-old; added=source-new",
            ],
        )

    def test_empty_snapshot_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no reference pods observed"):
            snapshot_pods({"items": []})

    def test_monitor_stops_only_the_pilot_when_reference_changes(self) -> None:
        snapshots = iter(
            [
                pod_payload(restarts=91),
                pod_payload(restarts=92),
            ]
        )
        stopped: list[list[str]] = []

        result = monitor_pilot(
            read_reference=lambda: next(snapshots),
            read_job_state=lambda: "running",
            stop_pilot=stopped.append,
            sleep=lambda _: None,
            interval_seconds=0,
        )

        self.assertEqual(result["verdict"], "STOPPED_REFERENCE_GUARD")
        self.assertEqual(
            stopped,
            [["reference pod source-abc restart count increased from 91 to 92"]],
        )

    def test_monitor_leaves_a_completed_pilot_untouched(self) -> None:
        stopped: list[list[str]] = []

        result = monitor_pilot(
            read_reference=lambda: pod_payload(),
            read_job_state=lambda: "complete",
            stop_pilot=stopped.append,
            sleep=lambda _: None,
            interval_seconds=0,
        )

        self.assertEqual(result["verdict"], "COMPLETE")
        self.assertEqual(stopped, [])

    def test_monitor_can_use_a_predeployment_baseline(self) -> None:
        reads: list[str] = []

        result = monitor_pilot(
            read_reference=lambda: reads.append("read") or pod_payload(),
            read_job_state=lambda: "complete",
            stop_pilot=lambda _: self.fail("completed pilot must not be stopped"),
            baseline_payload=pod_payload(restarts=91),
            sleep=lambda _: None,
            interval_seconds=0,
        )

        self.assertEqual(result["verdict"], "COMPLETE")
        self.assertEqual(reads, [])
