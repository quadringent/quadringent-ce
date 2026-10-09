"""La capture ne publie que les mutations déjà exécutées dans le scénario."""
from copy import deepcopy

import pytest

from quadringent_qualification.adapters import CaptureResult
from quadringent_qualification.orchestrator import Orchestrator

from .fakes import FakeCaptureRunner, FakeSourceDriver, FakeStorageBackend, FakeWarehouseLoader
from .test_orchestrator import make_config


class JournalSource(FakeSourceDriver):
    def __post_init__(self):
        super().__post_init__()
        self.journal = []

    def execute(self, statements):
        for statement in statements:
            before = deepcopy(self.rows)
            result = super().execute((statement,))
            if result.exit_code:
                return result
            keys = set(before) | set(self.rows)
            for key in keys:
                if before.get(key) != self.rows.get(key):
                    self.journal.append({
                        "event_id": f"{self.receiver}:{self.last_sequence}",
                        "receiver": self.receiver, "sequence": self.last_sequence,
                        "operation": "d" if key not in self.rows else "u_after" if key in before else "c",
                        "payload": {"before": before.get(key), "after": deepcopy(self.rows.get(key))},
                    })
        return result


class CausalCapture(FakeCaptureRunner):
    def __init__(self, source, warehouse, *, fail_last=False):
        super().__init__()
        self.source, self.warehouse = source, warehouse
        self.cursor = 0
        self.fail_last = fail_last
        self.published = []
        self.mirror = []
        self.positions_at_capture = []
        self.mirror_counts_at_capture = []

    def run(self, *, label, max_seconds, bootstrap, env):
        super().run(label=label, max_seconds=max_seconds, bootstrap=bootstrap, env=env)
        self.positions_at_capture.append((self.source.receiver, self.source.last_sequence))
        if self.fail_last and label == "capture-2":
            return CaptureResult(exit_code=5, events=(), log="capture failed")
        if label == "snapshot":
            self.published = [
                {"event_id": f"snapshot:{key}", "receiver": "SNAPSHOT:qual-test-1",
                 "sequence": key, "operation": "c", "payload": {"after": deepcopy(row)},
                 "is_snapshot": True}
                for key, row in self.source.rows.items()
            ]
        else:
            self.published.extend(deepcopy(self.source.journal[self.cursor:]))
        self.cursor = len(self.source.journal)
        self.warehouse.raw_rows = self.warehouse.raw_distinct = len(self.published)
        self.mirror = deepcopy(list(self.source.rows.values()))
        self.mirror_counts_at_capture.append(len(self.mirror))
        return CaptureResult(exit_code=0, events=(), log="receipted capture")


class ReceiptedWarehouse(FakeWarehouseLoader):
    def load(self, *, raw_prefix):
        super().load(raw_prefix=raw_prefix)
        self.events = deepcopy(self.capture.published)
        self.mirror_rows = deepcopy(self.capture.mirror)
        self.raw_rows = self.raw_distinct = len(self.events)


def scenario(*, fail_last=False):
    steps = ("seed", "snapshot", "changes1", "capture", "changes2", "rotate", "changes3", "reconcile")
    source = JournalSource()
    warehouse = ReceiptedWarehouse()
    capture = CausalCapture(source, warehouse, fail_last=fail_last)
    warehouse.capture = capture
    return Orchestrator(make_config(steps), source=source, capture=capture,
                        storage=FakeStorageBackend(), warehouse=warehouse), capture, warehouse, steps


def test_reconcile_captures_last_mutations_across_receiver_before_loading():
    orch, capture, warehouse, steps = scenario()
    report = orch.run(steps)
    assert report.status == "PASS"
    assert [step.name for step in report.steps] == list(steps)
    assert len(capture.calls) == 3  # snapshot puis deux captures, aucune donnée future
    assert capture.mirror_counts_at_capture == [100, 105, 110]
    assert capture.positions_at_capture[1][0] == "R1"
    assert capture.positions_at_capture[2][0] == "R2"
    assert any(event["receiver"] == "R2" for event in warehouse.events)
    assert len(warehouse.mirror_rows) == len(orch.source.rows) == 110
    assert report.reconciliation.journal_continuity.receivers == ("R1", "R2")
    assert report.reconciliation.journal_continuity.duplicate_positions == 0
    assert warehouse.load_calls == [orch.config.storage.raw_prefix]
    assert orch.run(("reconcile",)).status == "PASS"
    assert len(capture.calls) == 3  # source inchangée : pas de capture redondante


def test_pending_capture_failure_prevents_loader_and_reconciliation():
    orch, capture, warehouse, steps = scenario(fail_last=True)
    report = orch.run(steps)
    assert report.status == "FAIL"
    assert report.steps[-1].name == "reconcile"
    assert report.steps[-1].details["reason"] == "pending_capture_failed"
    assert report.reconciliation is None
    assert warehouse.load_calls == []
    capture.fail_last = False
    assert orch.run(("reconcile",)).status == "PASS"  # mutations restent en attente
    assert len(capture.calls) == 4


@pytest.mark.parametrize("steps", [("seed", "snapshot", "reconcile"), ("reconcile",)])
def test_reconcile_without_pending_writes_does_not_capture(steps):
    orch, capture, _, _ = scenario()
    assert orch.run(steps).status == "PASS"
    assert [call["label"] for call in capture.calls] == (["snapshot"] if "snapshot" in steps else [])
