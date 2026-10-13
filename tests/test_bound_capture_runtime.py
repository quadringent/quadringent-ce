from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import CapturedWindow, ContinuousCaptureService, ReceiverSnapshot
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.fleet_capture import FleetTableRouter, FleetWindowCoordinator, table_object_prefix
from quadringent.raw import RawBatchWriter, bind_raw_batch, bind_snapshot_batch, read_raw_batch
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity


IDENTITY = ReceiverIdentity("LIB", "JRN", "LIB", "RCVA", "ibmi-ifs-object-v1", "1:2:3:4")
BINDING = CaptureBinding("scope", "epoch", "ibmi", "JRN",
                         "s3://bucket/receiver-authority-v1",
                         BoundCheckpoint("scope", "epoch", IDENTITY, 1))


class Registry:
    def __init__(self):
        self.claims = {IDENTITY.receiver_name: IDENTITY}

    def require(self, binding, point):
        binding.validate_checkpoint(point)
        if self.claims.get(point.receiver_identity.receiver_name) != point.receiver_identity:
            raise ValueError("required receiver claim missing or different")

    def claim(self, binding, point):
        binding.validate_checkpoint(point)
        previous = self.claims.setdefault(point.receiver_identity.receiver_name, point.receiver_identity)
        if previous != point.receiver_identity:
            raise ValueError("receiver name reused")


class Catalog:
    def __init__(self, end=3):
        self.current = IDENTITY
        self.end = end
        self.identity_calls = 0
        self.snapshots = 0

    def identity(self, **request):
        assert request == dict(journal_library="LIB", journal_name="JRN",
                               receiver_library="LIB", receiver_name="RCVA", asp_number=1)
        self.identity_calls += 1
        return self.current

    def snapshot(self, required_receiver=None):
        assert required_receiver == "RCVA"
        self.snapshots += 1
        return [ReceiverSnapshot("LIB", "RCVA", 1, self.end, "ATTACHED")]


class Runner:
    def __init__(self, root, catalog, mutate=False, nonempty=False):
        self.root, self.catalog = root, catalog
        self.mutate, self.nonempty = mutate, nonempty
        self.calls = 0

    def capture(self, plan):
        self.calls += 1
        if self.mutate:
            self.catalog.current = replace(IDENTITY, native_generation="1:2:3:99")
        if not self.nonempty:
            return CapturedWindow(plan.end)
        event = ChangeEvent("ibmi", "JRN", "LIB", "TABLE", "c", plan.start,
                            "2026-10-13T00:00:00Z", "schema-v1", None, {"ID": 1})
        manifest = RawBatchWriter(self.root).write_batch([event], high_watermark=plan.end)
        return CapturedWindow(plan.end, json.dumps(manifest.to_record()).encode(),
                              (self.root / f"batch-{manifest.batch_id}.jsonl").read_bytes())


def runtime(tmp_path, *, end=3, mutate=False, nonempty=False):
    store = FileObjectStore(tmp_path / "raw")
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    registry = Registry()
    catalog = Catalog(end)
    runner = Runner(tmp_path / "java", catalog, mutate, nonempty)
    service = ContinuousCaptureService(catalog, runner, RawFirstCaptureCoordinator(store, checkpoint),
                                       checkpoint, max_entries=10, receipted_scans=True,
                                       capture_binding=BINDING, receiver_registry=registry,
                                       utc_now=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    return service, store, checkpoint, catalog, runner, registry


def test_bound_capture_starts_after_original_bootstrap_and_publishes_full_receipt(tmp_path):
    service, store, checkpoint, catalog, runner, _ = runtime(tmp_path, nonempty=True)
    assert service.run_once().status == "published"
    assert runner.calls == 1
    assert checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)
    receipt = json.loads(store.get(store.list_receipt_keys(10)[0]))
    assert receipt["format_version"] == "quadringent-scan-receipt-v3"
    assert receipt["previous"] == BINDING.bootstrap.to_dict()
    assert receipt["start"] == BINDING.checkpoint(IDENTITY, 2).to_dict()
    assert receipt["capture_binding"] == BINDING.to_dict()
    raw = receipt["raw"]
    batch = read_raw_batch(store.get(raw["manifest_key"]), store.get(raw["payload_key"]))
    assert batch.manifest.bound_high_watermark == checkpoint.load_bound()
    assert batch.manifest.capture_binding == BINDING
    assert batch.events[0].position == JournalPosition("RCVA", 2)
    assert catalog.identity_calls >= 4


@pytest.mark.parametrize("idle", [True, False])
def test_replacement_before_read_refuses_same_names_and_bounds(tmp_path, idle):
    service, store, checkpoint, catalog, runner, _ = runtime(tmp_path, end=1 if idle else 3)
    catalog.current = replace(IDENTITY, native_generation="1:2:3:99")
    with pytest.raises(ValueError):
        service.run_once()
    assert runner.calls == 0
    assert checkpoint.load_bound() is None
    assert not store.list_receipt_keys(10)


@pytest.mark.parametrize("nonempty", [True, False])
def test_replacement_during_capture_never_publishes_or_advances(tmp_path, nonempty):
    service, store, checkpoint, _, runner, _ = runtime(tmp_path, mutate=True, nonempty=nonempty)
    with pytest.raises(ValueError):
        service.run_once()
    assert runner.calls == 1
    assert checkpoint.load_bound() == BINDING.bootstrap
    assert not store.list_receipt_keys(10)
    assert not list(store.root.glob("batch-*"))


def test_idle_validates_identity_and_resumes_bound_checkpoint(tmp_path):
    service, store, checkpoint, catalog, runner, _ = runtime(tmp_path, end=1)
    assert service.run_once().status == "idle"
    assert checkpoint.load_bound() == BINDING.bootstrap
    assert catalog.identity_calls >= 2 and runner.calls == 0
    catalog.current = replace(IDENTITY, native_generation="1:2:3:99")
    with pytest.raises(ValueError):
        service.run_once()
    assert checkpoint.load_bound() == BINDING.bootstrap
    assert not store.list_receipt_keys(10)


def test_unreadable_original_boundary_does_not_seed_a_checkpoint(tmp_path):
    service, store, checkpoint, catalog, runner, _ = runtime(tmp_path, end=20)
    catalog.snapshot = lambda required_receiver=None: [
        ReceiverSnapshot("LIB", "RCVA", 10, 20, "ATTACHED")
    ]
    with pytest.raises(ValueError, match="checkpoint precedes current receiver start"):
        service.run_once()
    assert checkpoint.load_bound() is None
    assert runner.calls == 0
    assert not store.list_receipt_keys(10)


def test_missing_original_claim_or_legacy_checkpoint_fails_before_source_io(tmp_path):
    service, _, checkpoint, catalog, runner, registry = runtime(tmp_path)
    registry.claims.clear()
    with pytest.raises(ValueError):
        service.run_once()
    assert catalog.identity_calls == 0 and runner.calls == 0
    registry.claims["RCVA"] = IDENTITY
    checkpoint.commit(JournalPosition("RCVA", 1))
    with pytest.raises(ValueError):
        service.run_once()
    assert catalog.identity_calls == 0 and runner.calls == 0


def test_bound_raw_preserves_event_ids_and_rejects_cross_epoch_rebinding(tmp_path):
    service, store, _, _, _, _ = runtime(tmp_path, nonempty=True)
    service.run_once()
    receipt = json.loads(store.get(store.list_receipt_keys(10)[0]))
    manifest = store.get(receipt['raw']['manifest_key'])
    payload = store.get(receipt['raw']['payload_key'])
    batch = read_raw_batch(manifest, payload)
    assert batch.events[0].event_id == ChangeEvent.from_record(json.loads(payload)).event_id
    other = replace(BINDING, capture_epoch='other', bootstrap=replace(BINDING.bootstrap, capture_epoch='other'))
    with pytest.raises(ValueError, match='another binding'):
        bind_raw_batch(manifest, payload, binding=other, end=other.checkpoint(IDENTITY, 3))
    record = json.loads(manifest)
    record['bound_high_watermark']['receiver_identity']['native_generation'] = 'new'
    with pytest.raises(ValueError, match='batch identity mismatch'):
        read_raw_batch(json.dumps(record).encode(), payload)


def test_snapshot_has_virtual_namespace_and_exact_original_handoff(tmp_path):
    run_id = 'decb4f26-564e-44d1-83a1-3726e7b7e162'
    virtual = JournalPosition('SNAPSHOT:' + run_id, 1)
    event = ChangeEvent('ibmi', 'SNAPSHOT:LIB.TABLE', 'LIB', 'TABLE', 'c', virtual,
                        '2026-10-13T00:00:00Z', 'snapshot-v1', None, {'ID': 1})
    writer = RawBatchWriter(tmp_path)
    original = writer.write_batch([event], high_watermark=virtual)
    payload = (tmp_path / f'batch-{original.batch_id}.jsonl').read_bytes()
    manifest = bind_snapshot_batch(json.dumps(original.to_record()).encode(), payload,
                                   binding=BINDING, boundary=BINDING.bootstrap,
                                   pipeline_id='pipeline', table_id='table', run_id=run_id)
    bound = read_raw_batch(manifest, payload)
    assert bound.manifest.format_version == 'as400-snapshot-v2'
    assert bound.manifest.high_watermark == virtual
    assert bound.manifest.bound_high_watermark is None
    assert bound.manifest.copy_context['boundary'] == BINDING.bootstrap.to_dict()
    assert bound.events[0].event_id == event.event_id
    with pytest.raises(ValueError, match='virtual snapshot'):
        bind_snapshot_batch(json.dumps(original.to_record()).encode(), payload, binding=BINDING,
                            boundary=BINDING.bootstrap, pipeline_id='pipeline', table_id='table', run_id='different')


def test_bound_fleet_preserves_each_receipt_and_full_raw_binding_before_journal_cas(tmp_path):
    service, _, checkpoint, _, _, _ = runtime(tmp_path, nonempty=True)
    stores, checkpoints = {}, {}
    def store_for(prefix):
        return stores.setdefault(prefix, FileObjectStore(tmp_path / 'tables' / prefix))
    def checkpoint_for(key):
        return checkpoints.setdefault(key, JsonCheckpointStore(tmp_path / 'table-state' / (key.replace('/', '-') + '.json')))
    router = FleetTableRouter(root='fleet', tables=['TABLE', 'EMPTY'], store_factory=store_for,
                              checkpoint_factory=checkpoint_for)
    service.coordinator = FleetWindowCoordinator(service.coordinator, router)
    assert service.run_once().status == 'published'
    assert checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)
    for table in ('TABLE', 'EMPTY'):
        key = table_object_prefix('fleet', table)
        store = stores[key]
        receipt = json.loads(store.get(store.list_receipt_keys(10)[0]))
        assert receipt['capture_binding'] == BINDING.to_dict()
        assert receipt['end'] == checkpoint.load_bound().to_dict()
        assert receipt['event_count'] == (1 if table == 'TABLE' else 0)
        assert checkpoints[key].load_bound() == checkpoint.load_bound()
        if receipt['raw']:
            raw = receipt['raw']
            assert read_raw_batch(store.get(raw['manifest_key']), store.get(raw['payload_key'])).manifest.capture_binding == BINDING


def test_bound_fleet_late_table_cannot_skip_earlier_journal_coverage(tmp_path):
    service, store, checkpoint, _, _, _ = runtime(tmp_path, end=5, nonempty=True)
    checkpoint.compare_and_set_bound(None, BINDING.checkpoint(IDENTITY, 3))
    table_store = FileObjectStore(tmp_path / 'late-table')
    table_checkpoint = JsonCheckpointStore(tmp_path / 'late-table-checkpoint.json')
    router = FleetTableRouter(root='fleet', tables=['TABLE', 'EMPTY'], store_factory=lambda _: table_store,
                              checkpoint_factory=lambda _: table_checkpoint)
    service.coordinator = FleetWindowCoordinator(service.coordinator, router)
    with pytest.raises(RuntimeError, match='backfill required'):
        service.run_once()
    assert checkpoint.load_bound() == BINDING.checkpoint(IDENTITY, 3)
    assert table_checkpoint.load_bound() is None
    assert not store.list_receipt_keys(10) and not table_store.list_receipt_keys(10)


@pytest.mark.parametrize('invalid', ['out-of-range-event', 'reversed-range', 'foreign-generation',
                                     'predecessor-gap', 'missing-timestamp', 'naive-timestamp', 'journal-cursor'])
def test_invalid_bound_fleet_scan_does_not_create_table_receipts_or_cursors(tmp_path, invalid):
    service, store, checkpoint, _, _, _ = runtime(tmp_path)
    checkpoint.compare_and_set_bound(None, BINDING.checkpoint(IDENTITY, 9)
                                     if invalid == 'journal-cursor' else BINDING.bootstrap)
    stores, checkpoints = {}, {}
    def store_for(prefix):
        return stores.setdefault(prefix, FileObjectStore(tmp_path / 'tables' / prefix))
    def checkpoint_for(key):
        return checkpoints.setdefault(key, JsonCheckpointStore(tmp_path / 'table-state' / (key.replace('/', '-') + '.json')))
    coordinator = FleetWindowCoordinator(service.coordinator, FleetTableRouter(
        root='fleet', tables=['EMPTY', 'TABLE'], store_factory=store_for, checkpoint_factory=checkpoint_for))
    start, end = BINDING.checkpoint(IDENTITY, 2), BINDING.checkpoint(IDENTITY, 3)
    timestamp = datetime(2026, 10, 13, tzinfo=timezone.utc)
    manifest = payload = None
    if invalid == 'out-of-range-event':
        event = ChangeEvent('ibmi', 'JRN', 'LIB', 'TABLE', 'c', BINDING.bootstrap.position,
                            '2026-10-13T00:00:00Z', 'schema-v1', None, {'ID': 1})
        original = RawBatchWriter(tmp_path / 'java').write_batch([event], high_watermark=end.position)
        payload = (tmp_path / 'java' / f'batch-{original.batch_id}.jsonl').read_bytes()
        manifest = bind_raw_batch(json.dumps(original.to_record()).encode(), payload, binding=BINDING, end=end)
    elif invalid == 'reversed-range':
        start = BINDING.checkpoint(IDENTITY, 4)
    elif invalid == 'foreign-generation':
        end = replace(end, receiver_identity=replace(IDENTITY, native_generation='other'))
    elif invalid == 'predecessor-gap':
        start = BINDING.checkpoint(IDENTITY, 3)
    elif invalid == 'missing-timestamp':
        timestamp = None
    elif invalid == 'naive-timestamp':
        timestamp = datetime(2026, 10, 13)
    with pytest.raises((ValueError, RuntimeError)):
        coordinator.capture_receipted_window_result(
            start=start, end=end, previous=BINDING.bootstrap, capture_binding=BINDING,
            manifest_content=manifest, payload=payload, scan_completed_at=timestamp)
    assert all(point.load_bound() is None for point in checkpoints.values())
    assert all(not table_store.list_receipt_keys(10) for table_store in stores.values())
    assert not store.list_receipt_keys(10)
