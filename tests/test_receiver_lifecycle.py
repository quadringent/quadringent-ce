"""Synthetic receiver lifecycle refusals; no IBM i connection."""

from dataclasses import replace
import hashlib

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import (
    CapturedWindow, ContinuousCaptureService, ReceiverPlanningError, ReceiverSnapshot,
    finite_tail_bootstrap, plan_next_window,
)
from quadringent.contract import JournalPosition
from quadringent.java_catalog import CachedReceiverCatalog
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.qualification import collect, proof
from test_continuous import FakeCatalog, FakeRunner
from test_continuous import raw_bytes
from test_native_qualification_rotation import fixture as rotation_fixture
from test_native_qualification_rotation_collect import fixture as windows_fixture


UNREADABLE = ("PARTIAL", "FREED", "EMPTY", "DETACHED", "UNKNOWN", "", None)


@pytest.mark.parametrize("status", UNREADABLE)
@pytest.mark.parametrize("phase", ("resume", "idle", "bootstrap", "rotation"))
def test_unreadable_receiver_refuses_before_read_or_checkpoint(tmp_path, status, phase):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    previous = JournalPosition("R1", 10 if phase == "resume" else 20)
    if phase != "bootstrap":
        checkpoint.commit(previous)
    before = checkpoint.path.read_bytes() if checkpoint.path.exists() else None
    old = ReceiverSnapshot("S", "R1", 1, 20, status="ONLINE", next_receiver_library="S",
                           next_receiver="R2", chain_links_observed=True)
    rows = [replace(old, status=status)]
    end = JournalPosition("R1", 20)
    if phase == "rotation":
        rows = [old, ReceiverSnapshot("S", "R2", 21, 30, status=status,
                previous_receiver_library="S", previous_receiver="R1", chain_links_observed=True)]
        end = JournalPosition("R2", 30)
    runner = FakeRunner(CapturedWindow(scanned_to=end))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(
        FakeCatalog(rows), runner, RawFirstCaptureCoordinator(store, checkpoint), checkpoint,
        bootstrap=JournalPosition("R1", 1) if phase == "bootstrap" else None,
        receipted_scans=True, max_entries=20, sleep=lambda _: None,
    )

    with pytest.raises(ReceiverPlanningError, match="receiver.*status"):
        service.run_once()

    assert runner.windows == []
    assert (checkpoint.path.read_bytes() if checkpoint.path.exists() else None) == before
    assert list((tmp_path / "raw").rglob("*.json*")) == []
    assert service.metrics.empty_scans == service.metrics.idle_polls == 0
    assert service.metrics.batches_published == service.metrics.receiver_rotations == 0


@pytest.mark.parametrize("status", UNREADABLE)
def test_tail_bootstrap_requires_readable_native_status(status):
    with pytest.raises(ReceiverPlanningError, match="receiver.*status"):
        finite_tail_bootstrap([ReceiverSnapshot("S", "R1", 1, 20, status=status)], 10)


@pytest.mark.parametrize("status", ("ATTACHED", "ONLINE", "SAVED"))
def test_readable_native_status_preserves_bounded_scan(status):
    row = ReceiverSnapshot("S", "R1", 1, 20, status=status)
    plan = plan_next_window(JournalPosition("R1", 10), [row], max_entries=5)
    assert plan.start == JournalPosition("R1", 11)
    assert plan.end == JournalPosition("R1", 15)


def test_unrelated_older_partial_receiver_does_not_block_bound_current_scan():
    rows = [ReceiverSnapshot("S", "OLD", 1, 10, status="PARTIAL"),
            ReceiverSnapshot("S", "R1", 11, 20, status="ATTACHED")]
    assert plan_next_window(JournalPosition("R1", 15), rows, max_entries=5).end.sequence == 20


@pytest.mark.parametrize("change", (*UNREADABLE, "library", "first", "last", "missing", "duplicate"))
@pytest.mark.parametrize("raw", (False, True))
def test_metadata_changed_during_scan_never_publishes_or_advances(tmp_path, change, raw):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    checkpoint.commit(JournalPosition("R1", 10))
    before = checkpoint.path.read_bytes()
    initial = ReceiverSnapshot("S", "R1", 1, 20, status="ATTACHED")
    catalog = FakeCatalog([initial])
    if change == "library": changed = replace(initial, receiver_library="OTHER")
    elif change == "first": changed = replace(initial, first_sequence=2)
    elif change == "last": changed = replace(initial, last_sequence=19)
    else: changed = replace(initial, status=change if change in UNREADABLE else "ATTACHED")
    manifest, payload = raw_bytes(tmp_path / "staged", receiver="R1", sequence=11,
        high_watermark=JournalPosition("R1", 20)) if raw else (None, None)

    class ChangingRunner(FakeRunner):
        def capture(self, window):
            result = super().capture(window)
            catalog.snapshots = [] if change == "missing" else [changed]
            if change == "duplicate":
                catalog.snapshots.append(replace(changed, receiver_library="OTHER"))
            return result

    runner = ChangingRunner(CapturedWindow(JournalPosition("R1", 20), manifest, payload))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
        checkpoint, receipted_scans=True, max_entries=20, sleep=lambda _: None)
    with pytest.raises(ReceiverPlanningError):
        service.run_once()
    assert len(runner.windows) == 1
    assert checkpoint.path.read_bytes() == before
    assert list((tmp_path / "raw").rglob("*.json*")) == []
    assert service.metrics.empty_scans == service.metrics.batches_published == 0


def test_readable_receiver_can_grow_or_detach_during_bounded_scan(tmp_path):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    checkpoint.commit(JournalPosition("R1", 10))
    catalog = FakeCatalog([ReceiverSnapshot("S", "R1", 1, 20, status="ATTACHED")])

    class GrowingRunner(FakeRunner):
        def capture(self, window):
            result = super().capture(window)
            catalog.snapshots = [ReceiverSnapshot("S", "R1", 1, 25, status="ONLINE")]
            return result

    runner = GrowingRunner(CapturedWindow(JournalPosition("R1", 20)))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
        checkpoint, max_entries=20, sleep=lambda _: None)
    assert service.run_once().status == "empty_scan"
    assert checkpoint.load() == JournalPosition("R1", 20)


@pytest.mark.parametrize("status", ("PARTIAL", "FREED"))
@pytest.mark.parametrize("attached", (False, True))
def test_cached_metadata_cannot_mask_unreadable_status_after_scan(tmp_path, status, attached):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    checkpoint.commit(JournalPosition("R1", 10))
    row = ReceiverSnapshot("S", "R1", 1, 20, status="ATTACHED" if attached else "ONLINE")
    inner = FakeCatalog([row])
    catalog = CachedReceiverCatalog(inner, ttl_polls=30, ttl_seconds=60, clock=lambda: 0.0,
        tail_probe=lambda: inner.snapshots[-1] if inner.snapshots[-1].status == "ATTACHED" else None)

    class ChangingRunner(FakeRunner):
        def capture(self, window):
            result = super().capture(window)
            inner.snapshots = [replace(row, status=status)]
            return result

    runner = ChangingRunner(CapturedWindow(JournalPosition("R1", 20)))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
        checkpoint, receipted_scans=True, max_entries=20, sleep=lambda _: None)
    with pytest.raises(ReceiverPlanningError, match="receiver.*status"):
        service.run_once()
    assert checkpoint.load() == JournalPosition("R1", 10)
    assert len(inner.required) == 2
    assert list((tmp_path / "raw").rglob("*.json*")) == []


@pytest.mark.parametrize("status", ("PARTIAL", "FREED"))
def test_bootstrap_idle_cannot_reuse_stale_cached_status(tmp_path, status):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    row = ReceiverSnapshot("S", "R1", 1, 20, status="ATTACHED")
    inner = FakeCatalog([row])
    catalog = CachedReceiverCatalog(inner, ttl_polls=30, ttl_seconds=60, clock=lambda: 0.0,
        tail_probe=lambda: inner.snapshots[-1] if inner.snapshots[-1].status == "ATTACHED" else None)
    runner = FakeRunner(CapturedWindow(JournalPosition("R1", 20)))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
        checkpoint, bootstrap=JournalPosition("R1", 21), max_entries=20, sleep=lambda _: None)
    assert service.run_once().status == "idle"
    inner.snapshots = [replace(row, status=status)]
    with pytest.raises(ReceiverPlanningError, match="receiver.*status"):
        service.run_once()
    assert service.metrics.idle_polls == 1
    assert runner.windows == []
    assert checkpoint.load() is None
    assert list((tmp_path / "raw").rglob("*.json*")) == []


@pytest.mark.parametrize("change", ("status", "missing", "link", "bounds", "reattached"))
def test_rotation_rechecks_predecessor_before_checkpoint_transition(tmp_path, change):
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    previous = JournalPosition("R1", 20)
    checkpoint.commit(previous)
    old = ReceiverSnapshot("S", "R1", 1, 20, status="ONLINE", next_receiver_library="S",
                           next_receiver="R2", chain_links_observed=True)
    new = ReceiverSnapshot("S", "R2", 21, 30, status="ATTACHED", previous_receiver_library="S",
                           previous_receiver="R1", chain_links_observed=True)
    catalog = FakeCatalog([old, new])

    class ChangingRunner(FakeRunner):
        def capture(self, window):
            result = super().capture(window)
            if change == "status": catalog.snapshots = [replace(old, status="FREED"), new]
            elif change == "missing": catalog.snapshots = [new]
            elif change == "bounds": catalog.snapshots = [replace(old, last_sequence=25), new]
            elif change == "reattached": catalog.snapshots = [replace(old, status="ATTACHED"), new]
            else: catalog.snapshots = [old, replace(new, previous_receiver="OTHER")]
            return result

    runner = ChangingRunner(CapturedWindow(JournalPosition("R2", 30)))
    store = FileObjectStore(tmp_path / "raw")
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
        checkpoint, receipted_scans=True, max_entries=20, sleep=lambda _: None)
    with pytest.raises(ReceiverPlanningError):
        service.run_once()
    assert checkpoint.load() == previous
    assert catalog.required == ["R1", "R1"]
    assert list((tmp_path / "raw").rglob("*.json*")) == []


def _set_first_status(catalog, status):
    rows = catalog["output"].splitlines()
    fields = rows[1].split("\t")
    fields[3] = "-" if status is None else status
    rows[1] = "\t".join(fields)
    catalog["output"] = "\n".join(rows) + "\n"
    catalog["output_sha256"] = hashlib.sha256(catalog["output"].encode()).hexdigest()


@pytest.mark.parametrize("status", UNREADABLE)
def test_rotation_qualification_refuses_unreadable_retained_receiver(status):
    before, after, operations = rotation_fixture()
    _set_first_status(operations["catalog_after"], status)
    with pytest.raises(ValueError):
        proof.evaluate_rotation(before, after, operations)


@pytest.mark.parametrize("status", UNREADABLE)
def test_qualification_windows_refuse_unreadable_retained_receiver(status):
    args, boundary, checkpoint, catalog = windows_fixture()
    _set_first_status(catalog, status)
    with pytest.raises(ValueError):
        collect.receiver_windows(boundary, checkpoint, catalog, args)
