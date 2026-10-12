"""Synthetic native receiver-link checks; no IBM i connection."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import (
    CapturedWindow, ContinuousCaptureService, ReceiverPlanningError, ReceiverSnapshot, plan_next_window,
)
from quadringent.contract import JournalPosition
from quadringent.java_catalog import CachedReceiverCatalog, parse_catalog_output
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from test_continuous import FakeCatalog, FakeRunner, raw_bytes


V2 = (
    "as400-receiver-catalog-v2\n"
    "receiver\tQGPL\tR1\tONLINE\t100\t110\t-\t-\tQGPL\tR2\n"
    "receiver\tQGPL\tR2\tATTACHED\t111\t130\tQGPL\tR1\t-\t-\n"
)


def chain(first=111, last=130):
    return [
        ReceiverSnapshot("QGPL", "R1", 100, 110, next_receiver_library="QGPL",
                         next_receiver="R2", chain_links_observed=True, status="ONLINE"),
        ReceiverSnapshot("QGPL", "R2", first, last, previous_receiver_library="QGPL",
                         previous_receiver="R1", chain_links_observed=True, status="ONLINE"),
    ]


def test_native_v2_roundtrip_supplies_receiver_links():
    old, new = parse_catalog_output(V2)
    assert old.chain_links_observed and new.chain_links_observed
    assert (old.next_receiver_library, old.next_receiver) == ("QGPL", "R2")
    assert (new.previous_receiver_library, new.previous_receiver) == ("QGPL", "R1")
    assert old.previous_receiver is None and new.next_receiver is None


def test_legacy_catalog_is_readable_but_has_no_observed_links():
    row, = parse_catalog_output("as400-receiver-catalog-v1\nreceiver\tQGPL\tR1\tATTACHED\t100\t110\n")
    assert row.chain_links_observed is False
    assert row.previous_receiver is None and row.next_receiver is None
    assert plan_next_window(JournalPosition("R1", 100), [row], max_entries=10).end.sequence == 110


@pytest.mark.parametrize("row", [
    "receiver\tQGPL\tR1\tONLINE\t100\t110",
    "receiver\tQGPL\tR1\tONLINE\t100\t110\tQGPL\t-\t-\t-",
    "receiver\tQGPL\tR1\tONLINE\t100\t110\t-\t-\t-\tR2",
])
def test_v2_refuses_missing_columns_or_half_links(row):
    with pytest.raises(ValueError):
        parse_catalog_output("as400-receiver-catalog-v2\n" + row + "\n")


def test_legacy_header_cannot_claim_link_columns():
    with pytest.raises(ValueError):
        parse_catalog_output(V2.replace("catalog-v2", "catalog-v1"))


def test_reciprocal_continuous_rotation():
    plan = plan_next_window(JournalPosition("R1", 110), chain(), max_entries=10)
    assert plan.start == JournalPosition("R2", 111)
    assert plan.end == JournalPosition("R2", 120)
    assert plan.rotated and not plan.sequence_reset


def test_reciprocal_reset_rotation():
    plan = plan_next_window(JournalPosition("R1", 110), chain(1, 3), max_entries=10)
    assert plan.start == JournalPosition("R2", 1)
    assert plan.end == JournalPosition("R2", 3)
    assert plan.rotated and plan.sequence_reset


@pytest.mark.parametrize("change", ["previous-name", "previous-library", "next-name", "next-library",
                                    "unobserved", "following-unobserved", "missing-next", "missing-previous"])
def test_rotation_requires_both_native_link_identities(change):
    old, new = chain()
    if change == "previous-name": new = replace(new, previous_receiver="MISSING")
    elif change == "previous-library": new = replace(new, previous_receiver_library="OTHER")
    elif change == "next-name": old = replace(old, next_receiver="MISSING")
    elif change == "next-library": old = replace(old, next_receiver_library="OTHER")
    elif change == "following-unobserved": new = replace(new, chain_links_observed=False)
    elif change == "missing-next": old = replace(old, next_receiver_library=None, next_receiver=None)
    elif change == "missing-previous": new = replace(new, previous_receiver_library=None, previous_receiver=None)
    else: old = ReceiverSnapshot("QGPL", "R1", 100, 110, status="ONLINE")
    with pytest.raises(ReceiverPlanningError, match="receiver.*link|receiver.*match"):
        plan_next_window(JournalPosition("R1", 110), [old, new], max_entries=10)


def test_links_do_not_authorize_arbitrary_sequence_overlap():
    with pytest.raises(ReceiverPlanningError):
        plan_next_window(JournalPosition("R1", 110), chain(100, 120), max_entries=10)


def test_tail_probe_refresh_preserves_observed_native_links():
    inner = FakeCatalog(chain())
    catalog = CachedReceiverCatalog(inner, ttl_polls=5, ttl_seconds=60, clock=lambda: 0.0,
        tail_probe=lambda: ReceiverSnapshot("QGPL", "R2", 111, 140, status="ATTACHED"))
    # The probe carries bounds only; it must not replace the full catalog's links.
    inner.snapshots[1] = replace(inner.snapshots[1], status="ATTACHED")
    catalog.snapshot()
    rows = catalog.snapshot()
    assert len(inner.required) == 1
    assert rows[1].last_sequence == 140 and rows[1].chain_links_observed
    assert rows[1].previous_receiver == "R1"
    assert plan_next_window(JournalPosition("R1", 110), rows, max_entries=30).end.sequence == 140


@pytest.mark.parametrize("first", [1, 111])
def test_missing_intermediate_receiver_never_scans_or_advances(first):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint = JsonCheckpointStore(root / "checkpoint.json")
        checkpoint.commit(JournalPosition("R1", 110))
        before = checkpoint.path.read_bytes()
        old, new = chain(first, 130)
        new = replace(new, receiver="R3", previous_receiver="R2")
        runner = FakeRunner(CapturedWindow(JournalPosition("R3", first + 9)))
        service = ContinuousCaptureService(FakeCatalog([old, new]), runner,
            RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint), checkpoint, max_entries=10)
        with pytest.raises(ReceiverPlanningError):
            service.run_once()
        assert runner.windows == []
        assert checkpoint.path.read_bytes() == before
        assert service.metrics.batches_published == service.metrics.empty_scans == 0


def test_linked_reset_keeps_raw_before_checkpoint():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        class CheckedCheckpoint(JsonCheckpointStore):
            def transition(self, previous, position):
                assert list((root / "raw").glob("*.jsonl"))
                assert list((root / "raw").glob("*.manifest.json"))
                super().transition(previous, position)
        checkpoint = CheckedCheckpoint(root / "checkpoint.json")
        checkpoint.commit(JournalPosition("R1", 110))
        manifest, payload = raw_bytes(root / "input", receiver="R2", sequence=1,
                                      high_watermark=JournalPosition("R2", 3))
        service = ContinuousCaptureService(FakeCatalog(chain(1, 3)),
            FakeRunner(CapturedWindow(JournalPosition("R2", 3), manifest, payload)),
            RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint), checkpoint, max_entries=10)
        poll = service.run_once()
        assert poll.status == "published" and poll.window.sequence_reset
        assert checkpoint.load() == JournalPosition("R2", 3)


def test_reset_receipt_publication_failure_retains_checkpoint():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint = JsonCheckpointStore(root / "checkpoint.json")
        checkpoint.commit(JournalPosition("R1", 110))
        before = checkpoint.path.read_bytes()
        store = FileObjectStore(root / "raw")
        runner = FakeRunner(CapturedWindow(JournalPosition("R2", 3)))
        service = ContinuousCaptureService(FakeCatalog(chain(1, 3)), runner,
            RawFirstCaptureCoordinator(store, checkpoint), checkpoint, max_entries=10, receipted_scans=True)
        with patch.object(store, "put_once", side_effect=OSError("storage unavailable")):
            with pytest.raises(OSError):
                service.run_once()
        assert len(runner.windows) == 1
        assert checkpoint.path.read_bytes() == before
