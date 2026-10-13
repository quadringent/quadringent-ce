from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator, receipt_index_key
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent import proof_windows as proof

NOW = datetime(2026, 10, 13, tzinfo=UTC)
IDENTITY = ReceiverIdentity('JLIB', 'TRNJRN', 'RLIB', 'R1', 'test-native', 'generation-1')
BOOTSTRAP = BoundCheckpoint('scope-a', 'epoch-a', IDENTITY, 9)
BINDING = CaptureBinding('scope-a', 'epoch-a', 'ibmi', 'TRNJRN',
                         's3://test-receiver-registry/receiver-authority-v1', BOOTSTRAP)


def fixture(tmp_path):
    store = FileObjectStore(tmp_path / 'objects')
    checkpoint = JsonCheckpointStore(tmp_path / 'checkpoint.json')
    checkpoint.compare_and_set_bound(None, BOOTSTRAP)
    return store, checkpoint


def publish(store, checkpoint, *, previous=BOOTSTRAP, identity=IDENTITY, sequence=20, at=NOW):
    start = BINDING.checkpoint(identity, previous.sequence + 1 if identity == previous.receiver_identity else 1)
    end = BINDING.checkpoint(identity, sequence)
    RawFirstCaptureCoordinator(store, checkpoint).capture_receipted_window_result(
        start=start, end=end, previous=previous, scan_completed_at=at, capture_binding=BINDING)
    key = 'receipts/scan-' + hashlib.sha256(json.dumps(start.to_dict(), sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest() + '.json'
    return key, end


def test_bound_window_rotation_seals_full_identity_and_recovers(tmp_path):
    store, checkpoint = fixture(tmp_path)
    proof.prepare_window(store, checkpoint, window_id='w1', stream_id='test/run', now=NOW,
                         duration_seconds=600, capture_binding=BINDING)
    first_key, first = publish(store, checkpoint, at=NOW + timedelta(seconds=30))
    rotated = replace(IDENTITY, receiver_name='R2', native_generation='generation-2')
    second_key, end = publish(store, checkpoint, previous=first, identity=rotated,
                              sequence=3, at=NOW + timedelta(seconds=600))
    closed = proof.close_eligible_window(store, checkpoint, window_id='w1',
                                        now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    assert closed['end'] == end.to_dict()
    assert closed['intent']['capture_binding'] == BINDING.to_dict()
    assert [entry['key'] for entry in closed['receipts']] == [first_key, second_key]
    assert proof.read_closed_window(store, window_id='w1', capture_binding=BINDING) == closed


@pytest.mark.parametrize('mutation', ('scope', 'epoch', 'generation', 'domain', 'registry'))
def test_bound_window_refuses_foreign_receipt_even_with_valid_index(tmp_path, mutation):
    store, checkpoint = fixture(tmp_path)
    proof.prepare_window(store, checkpoint, window_id='w1', stream_id='test/run', now=NOW,
                         duration_seconds=600, capture_binding=BINDING)
    key, end = publish(store, checkpoint, at=NOW + timedelta(seconds=600))
    receipt = json.loads(store.get(key))
    if mutation in ('scope', 'epoch'):
        receipt['previous']['scope_id' if mutation == 'scope' else 'capture_epoch'] = 'foreign'
    elif mutation == 'generation':
        receipt['previous']['receiver_identity']['native_generation'] = 'replacement'
    else:
        receipt['capture_binding']['source_system' if mutation == 'domain' else 'registry_authority'] = (
            'other-domain' if mutation == 'domain' else 's3://other-receiver-registry/receiver-authority-v1')
    payload = json.dumps(receipt).encode()
    (store.root / key).write_bytes(payload)
    index = {'format_version': 'quadringent-scan-index-v2', 'receipt_key': key,
             'receipt_sha256': hashlib.sha256(payload).hexdigest()}
    (store.root / receipt_index_key(end)).write_bytes(json.dumps(index).encode())
    with pytest.raises(ValueError):
        proof.close_eligible_window(store, checkpoint, window_id='w1',
                                    now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    assert not (store.root / 'windows/w1/closed.json').exists()


def test_bound_window_refuses_legacy_checkpoint_before_intent(tmp_path):
    from quadringent.contract import JournalPosition
    store = FileObjectStore(tmp_path / 'objects')
    checkpoint = JsonCheckpointStore(tmp_path / 'checkpoint.json')
    checkpoint.commit(JournalPosition('R1', 9))
    with pytest.raises(ValueError):
        proof.prepare_window(store, checkpoint, window_id='w1', stream_id='test/run', now=NOW,
                             duration_seconds=600, capture_binding=BINDING)
    assert not (store.root / 'windows').exists()


def test_bound_receipt_requires_full_index_not_projection(tmp_path):
    store, checkpoint = fixture(tmp_path)
    key, end = publish(store, checkpoint)
    (store.root / receipt_index_key(end)).unlink()
    store.put_once(receipt_index_key(end.position), json.dumps({
        'format_version': 'quadringent-scan-index-v1', 'receipt_key': key,
        'receipt_sha256': hashlib.sha256(store.get(key)).hexdigest()}).encode())
    with pytest.raises((ValueError, FileNotFoundError)):
        proof.validate_bound_receipt(store, key, BINDING)


def test_bound_window_chain_preserves_binding_across_successor(tmp_path):
    store, checkpoint = fixture(tmp_path)
    proof.prepare_window(store, checkpoint, window_id='w1', stream_id='test/run', now=NOW,
                         duration_seconds=600, capture_binding=BINDING)
    proof.prepare_window_chain(store, checkpoint, initial_window_id='w1', window_count=2, capture_binding=BINDING)
    _, end = publish(store, checkpoint, at=NOW + timedelta(seconds=600))
    proof.close_eligible_window(store, checkpoint, window_id='w1', now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    successor = proof.prepare_successor_window(store, checkpoint, predecessor_id='w1',
                                               now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    assert successor['previous'] == end.to_dict()
    assert successor['capture_binding'] == BINDING.to_dict()
    result = proof.read_window_chain(store, now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    assert result['closed_window_ids'] == ['w1']
    assert result['capture_binding'] == BINDING.to_dict()
    with pytest.raises(ValueError):
        proof.read_window_chain(store, now=NOW + timedelta(seconds=600),
                                capture_binding=replace(BINDING, source_system='other-domain'))


def test_bound_closed_raw_requires_complete_binding_without_projection(tmp_path):
    from quadringent.closed_window_raw import collect_closed_window_raw
    from quadringent.window_destination import verify_closed_window_destination
    import site_fixture
    site = site_fixture.build_test_site()
    store, checkpoint = fixture(tmp_path)
    proof.prepare_window(store, checkpoint, window_id='w1', stream_id='test/run', now=NOW,
                         duration_seconds=600, capture_binding=BINDING)
    publish(store, checkpoint, at=NOW + timedelta(seconds=600))
    proof.close_eligible_window(store, checkpoint, window_id='w1', now=NOW + timedelta(seconds=600), capture_binding=BINDING)
    assert collect_closed_window_raw(store, window_id='w1', site=site, capture_binding=BINDING).event_count == 0
    result = verify_closed_window_destination(None, store, run_id='run1', window_id='w1',
                                               observed_at=NOW + timedelta(seconds=601), site=site, capture_binding=BINDING)
    assert result['destination']['state'] == 'not_tested'
    assert result['window']['end'] == BINDING.checkpoint(IDENTITY, 20).to_dict()
    with pytest.raises(ValueError):
        collect_closed_window_raw(store, window_id='w1', site=site,
                                   capture_binding=replace(BINDING, source_system='other'))


def test_bound_checkpoint_cannot_create_legacy_window_by_projection(tmp_path):
    store, checkpoint = fixture(tmp_path)
    with pytest.raises(ValueError, match='binding'):
        proof.begin_window(store, checkpoint, window_id='w1', stream_id='test/run',
                           started_at=NOW, duration_seconds=600)
    assert not (store.root / 'windows').exists()
