from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.raw import RawBatchWriter, bind_raw_batch, bind_snapshot_batch
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.qualification.verification_contract import Column, ReceiverWindow, VerificationScope
from quadringent.qualification.verification_ledger import DiskReconciler
from quadringent.qualification.verification_stored import (
    BatchReference, ObjectReference, StoredExportError, StoredLimits, StoredReadSet, export_stored,
)
from quadringent.qualification.verification_stream import FrameReader, StreamLimits, write_stream
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef, copy_intent_key, evidence_key


RUN = '12345678-1234-4234-8234-123456789abc'
STAMP = '2026-01-01T00:00:00Z'
ROOT = 'qualification/site'


def put(store, key, data):
    store.put_once(key, data)
    return ObjectReference(key, hashlib.sha256(data).hexdigest())


def record(scope, sequence, image, *, snapshot=False, operation='c'):
    receiver = 'SNAPSHOT:' + RUN if snapshot else 'RCV1'
    journal = 'SNAPSHOT:TESTLIB.ITEMS' if snapshot else 'JRN'
    before = image if operation in {'u_before', 'd'} else None
    after = image if operation in {'c', 'u_after'} else None
    return ChangeEvent('ibmi', journal, 'TESTLIB', 'ITEMS', operation,
                       JournalPosition(receiver, sequence), STAMP, 'schema-one', before, after)


def publish_batch(tmp_path, store, scope, events, *, snapshot=False, end=None):
    staging = tmp_path / ('stage-' + str(len(list(tmp_path.glob('stage-*')))))
    manifest = RawBatchWriter(staging).write_batch(events, high_watermark=events[-1].position)
    payload = (staging / f'batch-{manifest.batch_id}.jsonl').read_bytes()
    raw = (staging / f'batch-{manifest.batch_id}.manifest.json').read_bytes()
    if snapshot:
        raw = bind_snapshot_batch(raw, payload, binding=scope.capture_binding, boundary=scope.copy_boundary,
                                  pipeline_id=scope.pipeline_id, table_id=scope.table_id, run_id=RUN)
    else:
        raw = bind_raw_batch(raw, payload, binding=scope.capture_binding,
                             end=end or scope.capture_binding.checkpoint(scope.checkpoint.receiver_identity, events[-1].position.sequence))
    batch_id = json.loads(raw)['batch_id']
    prefix = ROOT + '/items/' + ('snapshot' if snapshot else 'journal')
    return BatchReference(put(store, prefix + f'/batch-{batch_id}.manifest.json', raw),
                          put(store, prefix + f'/batch-{batch_id}.jsonl', payload))


@pytest.fixture
def published(tmp_path):
    store = FileObjectStore(tmp_path / 'objects')
    identity = ReceiverIdentity('TESTLIB', 'JRN', 'TESTLIB', 'RCV1', 'offline-v1', 'generation-one')
    registry = ReceiverNameRegistry(FileObjectStore(tmp_path / 'objects' / 'receiver-authority-v1'),
                                    authority='s3://synthetic-raw/receiver-authority-v1', source_system='ibmi', journal='JRN')
    binding = registry.create_or_load_binding(scope_id='scope-one', identity=identity, sequence=20)
    end = binding.checkpoint(identity, 21)
    registry.claim(binding, end)
    scope = VerificationScope('pipeline-one', 'table-one', RUN, 'TESTLIB', 'ITEMS', 'DEST', 'TEST',
                              'ITEMS_HISTORY', 'ITEMS_MIRROR',
                              (Column('ID', 'int'), Column('LABEL', 'text'), Column('AMOUNT', 'decimal')),
                              ('ID',), binding, binding.bootstrap, end,
                              (ReceiverWindow(identity, 21, 21),), 1, 'b' * 40, '0.4.5')
    first = {'ID': 1, 'LABEL': 'Café  ', 'AMOUNT': '12345678901234567890.123456789012345678'}
    second = {'ID': 2, 'LABEL': None, 'AMOUNT': None}
    snapshot = publish_batch(tmp_path, store, scope, [record(scope, 1, first, snapshot=True)], snapshot=True)
    journal = publish_batch(tmp_path, store, scope, [record(scope, 21, second)])
    boundary = JournalBoundary('TESTLIB', 'RCV1', 20,
                               datetime(2026, 1, 1, tzinfo=timezone.utc), identity, binding, binding.bootstrap)
    evidence = InitialCopyEvidence(scope.pipeline_id, scope.table_id, RUN, boundary, 1,
                                   datetime(2026, 1, 1, tzinfo=timezone.utc),
                                   (SnapshotBatchRef(Path(snapshot.payload.key).name, Path(snapshot.manifest.key).name),))
    copy_ref = put(store, evidence_key(ROOT, scope.table_id, RUN), json.dumps(evidence.to_dict()).encode())
    intent = {'format_version': 'quadringent-copy-intent-v1', 'pipeline_id': scope.pipeline_id,
              'table_id': scope.table_id, 'run_id': RUN, 'boundary': boundary.to_dict()}
    intent_ref = put(store, 'receiver-authority-v1/' + copy_intent_key(scope.pipeline_id, scope.table_id, RUN),
                     json.dumps(intent).encode())
    read_set = StoredReadSet(scope.sha256, (snapshot, journal))
    return store, scope, copy_ref, intent_ref, read_set, first, second


def export(published, *, dataset='raw', **kwargs):
    store, scope, copy, intent, read_set, *_ = published
    out = BytesIO()
    summary = export_stored(store, scope, out, dataset=dataset, raw_prefix_root=ROOT,
                            initial_copy=copy, copy_intent=intent,
                            raw_read_set=read_set if dataset == 'raw' else None, **kwargs)
    reader = FrameReader(BytesIO(out.getvalue()), scope_sha256=scope.sha256, dataset=dataset)
    rows = list(reader)
    assert reader.summary == summary
    return rows, out.getvalue()


def test_real_published_batches_reach_ledger_without_rewriting_identity(published, tmp_path):
    snapshot, _ = export(published, dataset='snapshot')
    raw, _ = export(published)
    _, scope, *_, first, second = published
    assert snapshot[0]['LABEL'] == 'Café  '
    assert snapshot[0]['AMOUNT'] == first['AMOUNT']
    assert raw[1]['LABEL'] is None and raw[1]['AMOUNT'] is None
    assert raw[0]['EVENT_ID'] == record(scope, 1, first, snapshot=True).event_id
    assert raw[1]['EVENT_ID'] == record(scope, 21, second).event_id
    assert raw[0]['JOURNAL_SEQUENCE'] == '1'
    layers = {'source': [first, second], 'snapshot': snapshot, 'journal': raw[1:],
              'positions': [{'JOURNAL_RECEIVER': 'RCV1', 'JOURNAL_SEQUENCE': '21'}],
              'raw': raw, 'history': raw, 'mirror': [first, second]}
    with DiskReconciler(tmp_path / 'ledger', scope) as ledger:
        for name, rows in layers.items():
            stream = BytesIO()
            write_stream(stream, rows, scope_sha256=scope.sha256, dataset=name, started_at=STAMP, completed_at=STAMP)
            ledger.ingest(name, FrameReader(BytesIO(stream.getvalue()), scope_sha256=scope.sha256, dataset=name))
        assert ledger.finish()['state'] == 'verified'


def test_missing_read_set_never_claims_physical_completeness(published):
    store, scope, copy, intent, *_ = published
    output = BytesIO()
    with pytest.raises(StoredExportError):
        export_stored(store, scope, output, dataset='raw', raw_prefix_root=ROOT,
                      initial_copy=copy, copy_intent=intent)
    assert b'"kind":"end"' not in output.getvalue()


def test_raw_read_set_replays_are_preserved(published):
    store, scope, copy, intent, inventory, *_ = published
    duplicate = replace(inventory, batches=inventory.batches + (inventory.batches[1],))
    rows, _ = export((store, scope, copy, intent, duplicate))
    assert len(rows) == 3 and rows[1] == rows[2]


@pytest.mark.parametrize('which', ['initial_copy', 'copy_intent', 'manifest', 'payload'])
def test_object_corruption_or_hash_mismatch_has_no_footer(published, which):
    store, scope, copy, intent, inventory, *_ = published
    ref = {'initial_copy': copy, 'copy_intent': intent,
           'manifest': inventory.batches[1].manifest, 'payload': inventory.batches[1].payload}[which]
    store._path_for(ref.key).write_bytes(b'SYNTHETIC_PRIVATE_CANARY')
    output = BytesIO()
    with pytest.raises(StoredExportError) as error:
        export_stored(store, scope, output, dataset='raw', raw_prefix_root=ROOT,
                      initial_copy=copy, copy_intent=intent, raw_read_set=inventory)
    assert 'SYNTHETIC_PRIVATE_CANARY' not in str(error.value)
    assert b'"kind":"end"' not in output.getvalue()


@pytest.mark.parametrize('field', ['copy_run_id', 'pipeline_id', 'table_id'])
def test_foreign_run_or_scope_is_refused(published, field):
    store, scope, copy, intent, inventory, *_ = published
    changed = replace(scope, **{field: 'different'})
    with pytest.raises(StoredExportError):
        export((store, changed, copy, intent, replace(inventory, scope_sha256=changed.sha256)))


def test_read_set_must_include_original_snapshot(published):
    store, scope, copy, intent, inventory, *_ = published
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=inventory.batches[1:])))


@pytest.mark.parametrize('limits', [StoredLimits(max_object_bytes=20), StoredLimits(max_objects=1),
                                     StoredLimits(max_total_bytes=100)])
def test_stored_input_budgets_are_independent_of_frame_budgets(published, limits):
    with pytest.raises(StoredExportError):
        export(published, stored_limits=limits)


def test_frame_budget_prevents_footer(published):
    with pytest.raises(StoredExportError):
        export(published, limits=StreamLimits(max_rows=1))


def test_orphan_conflicting_batch_remains_visible_to_ledger(published, tmp_path):
    store, scope, copy, intent, inventory, first, second = published
    orphan = publish_batch(tmp_path, store, scope, [record(scope, 21, {**second, 'LABEL': 'conflict'})])
    rows, _ = export((store, scope, copy, intent, replace(inventory, batches=inventory.batches + (orphan,))))
    assert rows[-1]['EVENT_ID'] == rows[-2]['EVENT_ID'] and rows[-1]['LABEL'] != rows[-2]['LABEL']
    with DiskReconciler(tmp_path / 'conflict-ledger', scope) as ledger:
        data = BytesIO()
        write_stream(data, rows, scope_sha256=scope.sha256, dataset='raw', started_at=STAMP, completed_at=STAMP)
        ledger.ingest('raw', FrameReader(BytesIO(data.getvalue()), scope_sha256=scope.sha256, dataset='raw'))
        assert ledger.finish()['datasets']['raw']['conflicting_identity_count'] == 1


def test_all_native_images_are_exported_without_collapsing_updates(published, tmp_path):
    store, old, copy, intent, inventory, first, second = published
    identity = old.checkpoint.receiver_identity
    scope = replace(old, checkpoint=old.capture_binding.checkpoint(identity, 24),
                    receiver_windows=(ReceiverWindow(identity, 21, 24),))
    updated = {**first, 'LABEL': ''}
    events = [record(scope, 21, first, operation='u_before'), record(scope, 22, updated, operation='u_after'),
              record(scope, 23, second), record(scope, 24, updated, operation='d')]
    ref = publish_batch(tmp_path, store, scope, events)
    rows, _ = export((store, scope, copy, intent, StoredReadSet(scope.sha256, (inventory.batches[0], ref))))
    assert [r['OPERATION'] for r in rows] == ['c', 'u_before', 'u_after', 'c', 'd']
    assert [r['EVENT_ID'] for r in rows[1:]] == [e.event_id for e in events]
    assert rows[2]['LABEL'] == ''


def test_different_original_copy_frontier_is_refused(published):
    store, old, copy, intent, inventory, *_ = published
    scope = replace(old, copy_boundary=old.checkpoint,
                    receiver_windows=(ReceiverWindow(old.checkpoint.receiver_identity, 22, 21),))
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, scope_sha256=scope.sha256)))


def test_foreign_table_in_raw_object_is_not_filtered_out(published, tmp_path):
    store, scope, copy, intent, inventory, *_ = published
    foreign = replace(record(scope, 21, {'ID': 9, 'LABEL': 'other', 'AMOUNT': None}), table='OTHER')
    ref = publish_batch(tmp_path, store, scope, [foreign])
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=inventory.batches + (ref,))))


def test_extra_business_field_is_not_silently_discarded(published, tmp_path):
    store, scope, copy, intent, inventory, _, second = published
    ref = publish_batch(tmp_path, store, scope, [record(scope, 21, {**second, 'UNDECLARED': 'must-not-disappear'})])
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], ref))))


@pytest.mark.parametrize('bad', [1.25, True, {'nested': 1}, []])
def test_inexact_or_structured_business_numbers_are_refused(published, tmp_path, bad):
    store, scope, copy, intent, inventory, _, second = published
    ref = publish_batch(tmp_path, store, scope, [record(scope, 21, {**second, 'ID': bad})])
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], ref))))


def test_raw_payload_event_id_is_checked_instead_of_recalculated(published):
    from quadringent.raw import _batch_id
    store, scope, copy, intent, inventory, *_ = published
    ref = inventory.batches[1]
    manifest = json.loads(store.get_bounded(ref.manifest.key, 100000))
    payload = json.loads(store.get_bounded(ref.payload.key, 100000))
    payload['event_id'] = 'f' * 64
    body = (json.dumps(payload) + '\n').encode()
    manifest['payload_sha256'] = hashlib.sha256(body).hexdigest()
    manifest['batch_id'] = _batch_id(manifest['format_version'], manifest['event_ids'],
                                    scope.checkpoint.position, manifest['payload_sha256'],
                                    capture_binding=scope.capture_binding, bound_high_watermark=scope.checkpoint)
    prefix = ROOT + '/items/journal/batch-' + manifest['batch_id']
    mutated = BatchReference(put(store, prefix + '.manifest.json', json.dumps(manifest).encode()),
                             put(store, prefix + '.jsonl', body))
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], mutated))))


def with_receipt(published, tmp_path):
    from quadringent.checkpoint import JsonCheckpointStore
    from quadringent.object_store import receipt_index_key
    store, scope, copy, intent, inventory, *_ = published
    checkpoint = JsonCheckpointStore(tmp_path / 'checkpoint.json')
    checkpoint.compare_and_set_bound(None, scope.copy_boundary)
    journal = FileObjectStore(store.root / ROOT / 'items/journal')
    ref = inventory.batches[1]
    result = RawFirstCaptureCoordinator(journal, checkpoint).capture_receipted_window_result(
        start=scope.checkpoint, end=scope.checkpoint, previous=scope.copy_boundary,
        manifest_content=store.get_bounded(ref.manifest.key, 100000),
        payload=store.get_bounded(ref.payload.key, 100000),
        scan_completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), capture_binding=scope.capture_binding)
    assert result.receipt['event_count'] == 1
    index = json.loads(journal.get_bounded(receipt_index_key(scope.checkpoint), 10000))
    receipt = ObjectReference(ROOT + '/items/journal/' + index['receipt_key'], index['receipt_sha256'])
    linked = replace(ref, receipt=receipt)
    return store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], linked))


def test_complete_original_receipt_is_validated_when_available(published, tmp_path):
    rows, _ = export(with_receipt(published, tmp_path))
    assert len(rows) == 2


@pytest.mark.parametrize('include_rrn', [False, True])
def test_actual_java_producer_identity_and_reserved_metadata(published, tmp_path, include_rrn):
    repo = Path(__file__).resolve().parents[1]
    deps = repo / 'java/target/dependency'
    javac, java = shutil.which('javac'), shutil.which('java')
    if javac is None or java is None or not list(deps.glob('ibmi-journal-parsing-*.jar')):
        pytest.skip('Java 21 and the project Java dependency build are required')
    source = tmp_path / 'StoredFixture.java'
    source.write_text('''
package io.quadringent.as400;
import java.math.BigDecimal;
import java.nio.file.Path;
import java.sql.Types;
import java.util.*;
import io.debezium.ibmi.db2.journal.retrieve.JournalInfo;
import io.debezium.ibmi.db2.journal.retrieve.JournalEntryType;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.Structure;
import io.debezium.ibmi.db2.journal.retrieve.SchemaCacheIF.TableInfo;
public final class StoredFixture {
 public static void main(String[] args) throws Exception {
  var id = new SnapshotIdentity("TESTLIB","ITEMS","12345678-1234-4234-8234-123456789abc");
  Map<String,Object> image=new LinkedHashMap<>();
  image.put("ID",1L);image.put("LABEL","Café  ");
  image.put("AMOUNT",new BigDecimal("12345678901234567890.123456789012345678"));image.put("_rrn",1L);
  var snapshot=new RawCaptureWriter.RawEvent(id.eventId(1),"ibmi",id.journal(),"TESTLIB","ITEMS","c",
   id.receiver(),"SNAPSHOT","1","2026-01-01T00:00:00Z","snapshot-v1",null,image,"SNAPSHOT_ROW");
  new RawCaptureWriter(Path.of(args[0],"snapshot")).writeBatch(List.of(snapshot),new RawCaptureWriter.RawPosition(id.receiver(),"1"));
  var table=new TableInfo(List.of(new Structure("ID","BIGINT",8,19,Types.BIGINT,false,1,false),
   new Structure("LABEL","VARCHAR",30,0,Types.VARCHAR,true,2,false),
   new Structure("AMOUNT","DECIMAL",38,18,Types.DECIMAL,true,3,false)),List.of("ID"),null);
  var writer=new RawCaptureWriter(Path.of(args[0],"journal"));
  var event=writer.eventFromSql("ibmi",new JournalInfo("JRN","TESTLIB",false),"TESTLIB","ITEMS",
   JournalEntryType.ADD_ROW1,table,new Object[]{2L,null,null},"RCV1","TESTLIB","21","2026-01-01T00:00:00Z",2L);
  writer.writeBatch(List.of(event),new RawCaptureWriter.RawPosition("RCV1","21"));
 }
}''')
    classes = tmp_path / 'java-classes'
    command = [javac, '--release', '21', '-encoding', 'UTF-8', '-cp', str(deps / '*'), '-d', str(classes),
               str(repo / 'java/src/main/java/io/quadringent/as400/RawCaptureWriter.java'),
               str(repo / 'java/src/main/java/io/quadringent/as400/SnapshotIdentity.java'), str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=30, env={})
    subprocess.run([java, '-Xmx128m', '-cp', str(classes) + ':' + str(deps / '*'),
                    'io.quadringent.as400.StoredFixture', str(tmp_path / 'java-output')],
                   check=True, capture_output=True, timeout=15, env={})
    old_store, scope, old_copy, old_intent, *_ = published
    if include_rrn:
        scope = replace(scope, columns=scope.columns + (Column('_rrn', 'int'),))
    store = FileObjectStore(tmp_path / 'java-objects')
    for key in old_store.list_keys(100):
        if key.startswith('receiver-authority-v1/'):
            store.put_once(key, old_store.get_bounded(key, 100000))
    refs = []
    for kind in ('snapshot', 'journal'):
        stage = tmp_path / 'java-output' / kind
        manifest_path = next(stage.glob('*.manifest.json'))
        payload = next(stage.glob('*.jsonl')).read_bytes()
        raw = manifest_path.read_bytes()
        if kind == 'snapshot':
            raw = bind_snapshot_batch(raw, payload, binding=scope.capture_binding, boundary=scope.copy_boundary,
                                      pipeline_id=scope.pipeline_id, table_id=scope.table_id, run_id=RUN)
        else:
            raw = bind_raw_batch(raw, payload, binding=scope.capture_binding, end=scope.checkpoint)
        batch = json.loads(raw)['batch_id']
        prefix = ROOT + '/items/' + kind + '/batch-' + batch
        refs.append(BatchReference(put(store, prefix + '.manifest.json', raw), put(store, prefix + '.jsonl', payload)))
    evidence = InitialCopyEvidence.from_dict(json.loads(old_store.get_bounded(old_copy.key, 65536)))
    evidence = replace(evidence, snapshot_batches=(SnapshotBatchRef(Path(refs[0].payload.key).name,
                                                                   Path(refs[0].manifest.key).name),))
    copy = put(store, old_copy.key, json.dumps(evidence.to_dict()).encode())
    intent = ObjectReference(old_intent.key, old_intent.sha256)
    result, _ = export((store, scope, copy, intent, StoredReadSet(scope.sha256, tuple(refs))))
    assert [r['EVENT_ID'] for r in result] == [record(scope, 1, {}, snapshot=True).event_id,
                                             record(scope, 21, {}).event_id]
    assert result[0]['AMOUNT'] == '12345678901234567890.123456789012345678'
    assert result[0]['LABEL'] == 'Café  ' and result[1]['LABEL'] is None
    if include_rrn:
        assert [row['_rrn'] for row in result] == [1, 2]
    else:
        assert '_rrn' not in result[0] and '_rrn' not in result[1]
    assert b'"_rrn"' in store.get_bounded(refs[0].payload.key, 100000)


def test_pinned_receipt_cannot_change_between_integrity_check_and_validation(published, tmp_path):
    from quadringent.object_store import receipt_index_key
    store, scope, copy, intent, inventory = with_receipt(published, tmp_path)
    receipt_ref = inventory.batches[1].receipt
    changed = json.loads(store.get_bounded(receipt_ref.key, 65536))
    changed['scan_completed_at'] = '2026-01-01T00:00:01+00:00'
    body = json.dumps(changed).encode()
    index_key = ROOT + '/items/journal/' + receipt_index_key(scope.checkpoint)
    index = json.loads(store.get_bounded(index_key, 4096))
    index['receipt_sha256'] = hashlib.sha256(body).hexdigest()
    class ChangingStore:
        receipt_reads = 0
        def get_bounded(self, key, max_bytes):
            if key == receipt_ref.key:
                self.receipt_reads += 1
                if self.receipt_reads > 1:
                    return body
            if key == index_key:
                return json.dumps(index).encode()
            return store.get_bounded(key, max_bytes)
    with pytest.raises(StoredExportError):
        export((ChangingStore(), scope, copy, intent, inventory))


def test_same_receiver_name_with_another_native_generation_is_refused(published, tmp_path):
    store, scope, copy, intent, inventory, _, second = published
    identity = replace(scope.checkpoint.receiver_identity, native_generation='another-generation')
    end = scope.capture_binding.checkpoint(identity, 21)
    ref = publish_batch(tmp_path, store, scope, [record(scope, 21, second)], end=end)
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], ref))))


def test_missing_object_is_not_replaced_by_an_empty_population(published):
    store, scope, copy, intent, inventory, *_ = published
    store._path_for(inventory.batches[1].payload.key).unlink()
    with pytest.raises(StoredExportError):
        export(published)


def test_export_never_lists_a_prefix_or_silently_accepts_truncation(published):
    store, scope, copy, intent, inventory, *_ = published
    class NoListing:
        def get_bounded(self, key, maximum):
            return store.get_bounded(key, maximum)
        def list_keys(self, *_):
            raise AssertionError('listing is not an inventory authority')
        list_receipt_keys = list_keys
    rows, _ = export((NoListing(), scope, copy, intent, inventory))
    assert len(rows) == 2


def test_time_spent_reading_objects_counts_against_the_outer_budget(published, monkeypatch):
    import time
    store, scope, copy, intent, inventory, *_ = published
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    class SlowStore:
        def get_bounded(self, key, maximum):
            value = store.get_bounded(key, maximum)
            clock[0] += 2
            return value
    with pytest.raises(StoredExportError):
        export((SlowStore(), scope, copy, intent, inventory), limits=StreamLimits(max_seconds=1))


@pytest.mark.parametrize('field,value', [('journal_receiver_library', 'OTHERLIB'),
                                         ('journal_entry_type', 'BEFORE_IMAGE')])
def test_known_native_envelope_metadata_is_checked(published, field, value):
    from quadringent.raw import _batch_id
    store, scope, copy, intent, inventory, *_ = published
    ref = inventory.batches[1]
    manifest = json.loads(store.get_bounded(ref.manifest.key, 100000))
    payload = json.loads(store.get_bounded(ref.payload.key, 100000))
    payload.update(journal_receiver_library='TESTLIB', journal_entry_type='ADD_ROW1')
    payload[field] = value
    body = (json.dumps(payload) + '\n').encode()
    manifest['payload_sha256'] = hashlib.sha256(body).hexdigest()
    manifest['batch_id'] = _batch_id(manifest['format_version'], manifest['event_ids'], scope.checkpoint.position,
        manifest['payload_sha256'], capture_binding=scope.capture_binding, bound_high_watermark=scope.checkpoint)
    prefix = ROOT + '/items/journal/batch-' + manifest['batch_id']
    ref = BatchReference(put(store, prefix + '.manifest.json', json.dumps(manifest).encode()),
                          put(store, prefix + '.jsonl', body))
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, batches=(inventory.batches[0], ref))))


@pytest.mark.parametrize('kwargs', [{'max_objects': True}, {'max_object_bytes': 0},
                                   {'max_total_bytes': 16 * 1024**3 + 1}, {'max_record_bytes': -1}])
def test_invalid_stored_limits_are_refused(kwargs):
    with pytest.raises(ValueError):
        StoredLimits(**kwargs)


def test_declared_rrn_cannot_be_invented_when_original_images_lack_it(published):
    store, old, copy, intent, inventory, first, second = published
    scope = replace(old, columns=old.columns + (Column('_rrn', 'int'),))
    # Original snapshot lacking a declared field must fail, not invent RRN.
    with pytest.raises(StoredExportError):
        export((store, scope, copy, intent, replace(inventory, scope_sha256=scope.sha256)))


def replace_original_snapshot(published, transform):
    from quadringent.raw import _batch_id
    store, scope, copy, intent, inventory, *_ = published
    ref = inventory.batches[0]
    manifest = json.loads(store.get_bounded(ref.manifest.key, 100000))
    payload = store.get_bounded(ref.payload.key, 100000)
    manifest, payload = transform(manifest, payload)
    manifest['payload_sha256'] = hashlib.sha256(payload).hexdigest()
    watermark = JournalPosition(**manifest['high_watermark'])
    manifest['batch_id'] = _batch_id(manifest['format_version'], manifest['event_ids'], watermark,
        manifest['payload_sha256'], capture_binding=scope.capture_binding, copy_context=manifest['copy_context'])
    prefix = ROOT + '/items/snapshot/batch-' + manifest['batch_id']
    new = BatchReference(put(store, prefix + '.manifest.json', json.dumps(manifest).encode()),
                         put(store, prefix + '.jsonl', payload))
    evidence = InitialCopyEvidence.from_dict(json.loads(store.get_bounded(copy.key, 65536)))
    evidence = replace(evidence, snapshot_batches=(SnapshotBatchRef(Path(new.payload.key).name,
                                                                    Path(new.manifest.key).name),))
    body = json.dumps(evidence.to_dict()).encode()
    store._path_for(copy.key).write_bytes(body)
    return store, scope, ObjectReference(copy.key, hashlib.sha256(body).hexdigest()), intent, inventory


def test_snapshot_manifest_watermark_cannot_invent_a_later_ordinal(published):
    def change(manifest, payload):
        manifest['high_watermark']['sequence'] = 2
        return manifest, payload
    with pytest.raises(StoredExportError):
        export(replace_original_snapshot(published, change), dataset='snapshot')


@pytest.mark.parametrize('transform', [lambda body: body[:-1], lambda body: body + b'\n',
                                      lambda body: body.replace(b'"ID":1', b'"ID":1,"ID":9')])
def test_malformed_original_jsonl_is_refused_even_with_consistent_hashes(published, transform):
    with pytest.raises(StoredExportError):
        export(replace_original_snapshot(published, lambda manifest, body: (manifest, transform(body))), dataset='snapshot')


@pytest.mark.parametrize('footer_time', [0.9, 1.5])
def test_total_deadline_includes_metadata_and_final_footer_write(published, monkeypatch, footer_time):
    import time
    store, scope, copy, intent, inventory, *_ = published
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    class TimedStore:
        def get_bounded(self, key, maximum):
            value = store.get_bounded(key, maximum)
            if key == copy.key:
                clock[0] = 0.75
            return value
    class DelayedFooter(BytesIO):
        def write(self, value):
            if b'"kind":"end"' in bytes(value):
                clock[0] = footer_time
            return super().write(value)
    output = DelayedFooter()
    def call():
        return export_stored(TimedStore(), scope, output, dataset='raw', raw_prefix_root=ROOT,
                             initial_copy=copy, copy_intent=intent, raw_read_set=inventory,
                             limits=StreamLimits(max_seconds=1))
    if footer_time > 1:
        with pytest.raises(StoredExportError):
            call()
        # A written footer cannot retract a failed producer return.
        assert b'"kind":"end"' in output.getvalue()
    else:
        assert call().row_count == 2


@pytest.mark.parametrize('flush_time', [0.9, 1.5])
def test_total_deadline_includes_final_output_flush(published, monkeypatch, flush_time):
    import time
    store, scope, copy, intent, inventory, *_ = published
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    class TimedStore:
        def get_bounded(self, key, maximum):
            value = store.get_bounded(key, maximum)
            if key == copy.key:
                clock[0] = 0.75
            return value
    class DelayedFlush(BytesIO):
        flush_calls = 0
        def flush(self):
            self.flush_calls += 1
            clock[0] = flush_time
            return super().flush()
    output = DelayedFlush()
    def call():
        return export_stored(TimedStore(), scope, output, dataset='raw', raw_prefix_root=ROOT,
                             initial_copy=copy, copy_intent=intent, raw_read_set=inventory,
                             limits=StreamLimits(max_seconds=1))
    if flush_time > 1:
        with pytest.raises(StoredExportError):
            call()
        assert b'"kind":"end"' in output.getvalue()
    else:
        assert call().row_count == 2
    assert output.flush_calls == 1


def test_flush_failure_is_sanitized_and_never_returns_a_success(published):
    store, scope, copy, intent, inventory, *_ = published
    class BrokenFlush(BytesIO):
        def flush(self):
            raise OSError('SYNTHETIC_PRIVATE_FLUSH_CANARY')
    output = BrokenFlush()
    with pytest.raises(StoredExportError) as failure:
        export_stored(store, scope, output, dataset='raw', raw_prefix_root=ROOT,
                      initial_copy=copy, copy_intent=intent, raw_read_set=inventory)
    assert 'SYNTHETIC_PRIVATE_FLUSH_CANARY' not in str(failure.value)
    assert failure.value.__cause__ is None
    assert b'"kind":"end"' in output.getvalue()
