from dataclasses import replace
import hashlib
import json
from unittest.mock import Mock

import pytest

import quadringent_destination_loader as loader
from quadringent.contract import JournalPosition
from quadringent.object_store import RawFirstCaptureCoordinator, receipt_index_key
from quadringent.raw import bind_raw_batch, bind_snapshot_batch, RawBatchWriter
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef, copy_intent_key
from test_bound_proof_windows import BINDING, BOOTSTRAP, IDENTITY, NOW
from test_continuous import raw_bytes
from test_destination_loader import _ReceiptCacheStorage


def setup(tmp_path, *, evidence=True, boundary_point=BOOTSTRAP):
    storage = _ReceiptCacheStorage(tmp_path)
    storage.kind, storage.raw_bucket = 'aws', 'test-receiver-registry'
    table = loader.LoaderTable('table-1', 'LEDGER', 'CNTR', ('ID',),
                               ({'name': 'ID', 'kind': 'integer', 'nullable': False},),
                               'raw/table-1/evidence/run-1.json', 'pipeline-1', 'run-1', BINDING)
    boundary = JournalBoundary('RLIB', 'R1', boundary_point.sequence, NOW, IDENTITY, BINDING, boundary_point)
    registry = ReceiverNameRegistry(storage.object_store('receiver-authority-v1'),
                                     authority=BINDING.registry_authority, source_system='ibmi', journal='TRNJRN')
    registry.store.put_once(registry.binding_key(BINDING.scope_id), json.dumps(BINDING.to_dict()).encode())
    registry._claim_point(BINDING, BOOTSTRAP)
    registry.store.put_once(copy_intent_key(table.pipeline_id, table.table_id, table.run_id), json.dumps({
        'format_version': 'quadringent-copy-intent-v1', 'pipeline_id': table.pipeline_id,
        'table_id': table.table_id, 'run_id': table.run_id, 'boundary': boundary.to_dict()}).encode())
    proof = InitialCopyEvidence('pipeline-1', 'table-1', 'run-1', boundary, 0, NOW)
    if evidence:
        storage.object_store('').put_once(table.evidence_key, json.dumps(proof.to_dict()).encode())
    return storage, table, proof


def load(storage, table, cursor):
    return loader.load_table_once(table, plan=loader.build_plan(table, database='D', schema='S'),
                                  storage=storage, raw_prefix_root='raw', streaming_client_factory=None,
                                  cursor=cursor, history_mode='sql')


def receipt(storage, tmp_path, *, binding=BINDING, manifest_mutation=None):
    source_checkpoint = storage.checkpoint_store('capture')
    source_checkpoint.compare_and_set_bound(None, binding.bootstrap)
    start, end = binding.checkpoint(IDENTITY, 10), binding.checkpoint(IDENTITY, 20)
    manifest, payload = raw_bytes(tmp_path / 'source', receiver='R1', sequence=12,
                                  high_watermark=JournalPosition('R1', 20))
    manifest = bind_raw_batch(manifest, payload, binding=binding, end=end)
    store = storage.object_store('raw/cntr/journal')
    RawFirstCaptureCoordinator(store, source_checkpoint).capture_receipted_window_result(
        start=start, end=end, previous=binding.bootstrap, manifest_content=manifest, payload=payload,
        scan_completed_at=NOW, capture_binding=binding)
    key = store.list_receipt_keys(10)[0]
    if manifest_mutation:
        content = json.loads(store.get(key))
        manifest_path = store.root / content['raw']['manifest_key']
        document = json.loads(manifest_path.read_bytes())
        manifest_mutation(document)
        altered = json.dumps(document).encode()
        manifest_path.write_bytes(altered)
        content['raw']['manifest_sha256'] = hashlib.sha256(altered).hexdigest()
        encoded = json.dumps(content).encode()
        (store.root / key).write_bytes(encoded)
        (store.root / receipt_index_key(end)).write_bytes(json.dumps({
            'format_version': 'quadringent-scan-index-v2', 'receipt_key': key,
            'receipt_sha256': hashlib.sha256(encoded).hexdigest()}).encode())
    return end


@pytest.mark.parametrize('field', ('pipeline_id', 'table_id', 'run_id'))
def test_bound_copy_identity_mismatch_fails_before_dml(tmp_path, field):
    storage, table, proof = setup(tmp_path)
    altered = replace(proof, **{field: 'other'})
    (storage.object_store('').root / table.evidence_key).write_bytes(json.dumps(altered.to_dict()).encode())
    cursor = Mock()
    with pytest.raises(loader.DestinationLoaderError):
        load(storage, table, cursor)
    cursor.execute.assert_not_called()
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key)).load() is None


@pytest.mark.parametrize('field', ('scope_id', 'capture_epoch', 'source_system'))
def test_bound_receipt_wrong_binding_fails_before_snapshot_delete(tmp_path, field):
    storage, table, _ = setup(tmp_path)
    changed = {field: 'other'}
    if field in ('scope_id', 'capture_epoch'):
        changed['bootstrap'] = replace(BOOTSTRAP, **{field: 'other'})
    foreign = replace(BINDING, **changed)
    if field == 'source_system':
        # Empty scan can carry a foreign EVENT_ID domain without RAW.
        checkpoint = storage.checkpoint_store('capture')
        checkpoint.compare_and_set_bound(None, foreign.bootstrap)
        RawFirstCaptureCoordinator(storage.object_store('raw/cntr/journal'), checkpoint).capture_receipted_window_result(
            start=foreign.checkpoint(IDENTITY, 10), end=foreign.checkpoint(IDENTITY, 20),
            previous=foreign.bootstrap, scan_completed_at=NOW, capture_binding=foreign)
    else:
        receipt(storage, tmp_path, binding=foreign)
    cursor = Mock()
    with pytest.raises(loader.DestinationLoaderError):
        load(storage, table, cursor)
    cursor.execute.assert_not_called()


@pytest.mark.parametrize('mutation', (
    lambda doc: doc.update(format_version='as400-raw-v1'),
    lambda doc: doc['bound_high_watermark']['receiver_identity'].update(native_generation='replacement'),
    lambda doc: doc['capture_binding'].update(source_system='foreign'),
))
def test_bound_raw_mismatch_fails_before_any_dml(tmp_path, mutation):
    storage, table, _ = setup(tmp_path)
    receipt(storage, tmp_path, manifest_mutation=mutation)
    cursor = Mock()
    with pytest.raises((loader.DestinationLoaderError, ValueError)):
        load(storage, table, cursor)
    cursor.execute.assert_not_called()


def test_bound_valid_empty_copy_then_raw_uses_full_checkpoint(tmp_path):
    storage, table, _ = setup(tmp_path)
    end = receipt(storage, tmp_path)
    cursor = Mock(rowcount=1)
    cursor.fetchall.return_value = []
    processed, appended = load(storage, table, cursor)
    assert (processed, appended) == (1, 1)
    checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    assert checkpoint.load_bound() == end
    assert load(storage, table, cursor) == (0, 0)


def test_installed_config_requires_bound_but_explicit_legacy_replay_remains(tmp_path):
    from test_destination_loader import _TABLE_SET_JSON
    assert loader.parse_table_set(_TABLE_SET_JSON)
    with pytest.raises(loader.DestinationLoaderError):
        loader.parse_table_set(_TABLE_SET_JSON, require_bound=True)


def test_bound_stale_evidence_rechecked_even_when_loader_is_idle(tmp_path):
    storage, table, proof = setup(tmp_path)
    checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    checkpoint.compare_and_set_bound(None, BOOTSTRAP)
    (storage.object_store('').root / table.evidence_key).write_bytes(json.dumps(replace(proof, run_id='old-run').to_dict()).encode())
    cursor = Mock()
    with pytest.raises(loader.DestinationLoaderError):
        load(storage, table, cursor)
    cursor.execute.assert_not_called()


def test_bound_streaming_is_refused_before_channel_or_dml(tmp_path):
    storage, table, _ = setup(tmp_path)
    cursor, client = Mock(), Mock()
    with pytest.raises(loader.DestinationLoaderError, match='SQL history'):
        loader.load_table_once(table, plan=loader.build_plan(table, database='D', schema='S'),
                               storage=storage, raw_prefix_root='raw', streaming_client_factory=client,
                               cursor=cursor, history_mode='streaming')
    cursor.execute.assert_not_called()
    client.assert_not_called()


def test_copy_boundary_inside_receipt_filters_only_full_native_identity(tmp_path):
    boundary = BINDING.checkpoint(IDENTITY, 15)
    storage, table, _ = setup(tmp_path, boundary_point=boundary)
    checkpoint = storage.checkpoint_store('capture')
    checkpoint.compare_and_set_bound(None, BOOTSTRAP)
    from test_continuous import event
    source = tmp_path / 'source'
    manifest = RawBatchWriter(source).write_batch([event('R1', 12), event('R1', 18)],
                                                high_watermark=JournalPosition('R1', 20))
    payload = (source / f'batch-{manifest.batch_id}.jsonl').read_bytes()
    end = BINDING.checkpoint(IDENTITY, 20)
    encoded = bind_raw_batch((source / f'batch-{manifest.batch_id}.manifest.json').read_bytes(), payload,
                              binding=BINDING, end=end)
    RawFirstCaptureCoordinator(storage.object_store('raw/cntr/journal'), checkpoint).capture_receipted_window_result(
        start=BINDING.checkpoint(IDENTITY, 10), end=end, previous=BOOTSTRAP,
        manifest_content=encoded, payload=payload, scan_completed_at=NOW, capture_binding=BINDING)
    cursor = Mock(rowcount=1)
    cursor.fetchall.return_value = []
    assert load(storage, table, cursor) == (1, 1)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert event('R1', 12).event_id not in '\n'.join(statements)
    assert event('R1', 18).event_id in '\n'.join(statements)
    target = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    assert target.load_bound() == end


def test_copy_boundary_ahead_of_capture_waits_without_replaying_old_updates(tmp_path):
    boundary = BINDING.checkpoint(IDENTITY, 30)
    storage, table, _ = setup(tmp_path, boundary_point=boundary)
    receipt(storage, tmp_path)
    cursor = Mock(rowcount=1)
    assert load(storage, table, cursor) == (0, 0)
    plan = loader.build_plan(table, database='D', schema='S')
    assert [call.args[0] for call in cursor.execute.call_args_list] == [f'DELETE FROM {plan.qualified_mirror_table}']
    target = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    assert target.load_bound() == boundary
    cursor.reset_mock()
    assert load(storage, table, cursor) == (0, 0)
    cursor.execute.assert_not_called()


def test_copy_evidence_boundary_must_match_immutable_intent_even_same_name(tmp_path):
    storage, table, proof = setup(tmp_path)
    new_point = BINDING.checkpoint(IDENTITY, 15)
    altered = replace(proof, boundary=JournalBoundary('RLIB', 'R1', 15, NOW, IDENTITY, BINDING, new_point))
    (storage.object_store('').root / table.evidence_key).write_bytes(json.dumps(altered.to_dict()).encode())
    cursor = Mock()
    with pytest.raises(loader.DestinationLoaderError):
        load(storage, table, cursor)
    cursor.execute.assert_not_called()


@pytest.mark.parametrize('foreign_context', (False, True))
def test_bound_snapshot_context_is_admitted_before_dml(tmp_path, foreign_context):
    from test_continuous import event
    from quadringent.storage_layout import snapshot_prefix
    storage, table, proof = setup(tmp_path)
    source = tmp_path / 'snapshot'
    row = replace(event('R1', 1), position=JournalPosition('SNAPSHOT:' + table.run_id, 1),
                  journal='SNAPSHOT:LEDGER.CNTR')
    manifest = RawBatchWriter(source).write_batch([row], high_watermark=row.position)
    payload = (source / f'batch-{manifest.batch_id}.jsonl').read_bytes()
    bound = bind_snapshot_batch((source / f'batch-{manifest.batch_id}.manifest.json').read_bytes(), payload,
                                 binding=BINDING, boundary=BOOTSTRAP,
                                 pipeline_id='other-pipeline' if foreign_context else table.pipeline_id,
                                 table_id=table.table_id, run_id=table.run_id)
    record = json.loads(bound)
    batch_id = record['batch_id']
    store = storage.object_store(snapshot_prefix('raw', table.table_name))
    store.put_once(f'batch-{batch_id}.jsonl', payload)
    store.put_once(f'batch-{batch_id}.manifest.json', bound)
    proof = replace(proof, rows_copied=1, snapshot_batches=(SnapshotBatchRef(
        f'batch-{batch_id}.jsonl', f'batch-{batch_id}.manifest.json'),))
    (storage.object_store('').root / table.evidence_key).write_bytes(json.dumps(proof.to_dict()).encode())
    cursor = Mock(rowcount=1)
    cursor.fetchall.return_value = []
    if foreign_context:
        with pytest.raises(loader.DestinationLoaderError):
            load(storage, table, cursor)
        cursor.execute.assert_not_called()
    else:
        assert load(storage, table, cursor) == (0, 0)
        assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(
            table.table_id, table.evidence_key)).load_bound() == BOOTSTRAP


def test_arbitrary_checkpoint_inside_receipt_fails_before_ddl(tmp_path):
    storage, table, _ = setup(tmp_path)
    receipt(storage, tmp_path)
    checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key))
    checkpoint.compare_and_set_bound(None, BINDING.checkpoint(IDENTITY, 15))
    cursor = Mock()
    with pytest.raises(loader.DestinationLoaderError):
        loader.run_once(storage=storage, tables=(table,), raw_prefix_root='raw', database='D', schema='S',
                        streaming_client_factory=None, cursor=cursor, history_mode='sql')
    cursor.execute.assert_not_called()


def test_installed_streaming_refusal_precedes_remote_connection(tmp_path, monkeypatch):
    from test_destination_loader import _installed_table_set_json
    monkeypatch.setenv('QUADRINGENT_HISTORY_MODE', 'streaming')
    monkeypatch.setenv('QUADRINGENT_LOADER_TABLE_SET_JSON', _installed_table_set_json())
    connection = Mock()
    monkeypatch.setattr(loader, '_connect_snowflake', connection)
    with pytest.raises(loader.DestinationLoaderError, match='SQL history'):
        loader.main(['--once'])
    connection.assert_not_called()
