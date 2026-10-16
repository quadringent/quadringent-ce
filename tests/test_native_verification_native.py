"""Native journal inputs come from the original v3 copy, never a plan verdict."""
from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import io
import struct

import pytest

from quadringent.qualification.verification_contract import Column, ReceiverWindow, VerificationScope
from quadringent.qualification.verification_native import load_native_copy_inputs, native_journal_processes
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence
from test_copy_observation import context, encode, observation, published


@pytest.fixture
def native_inputs():
    store, reference = published()
    current = context()
    boundary = JournalBoundary.from_dict(current['boundary'])
    point = boundary.require_bound()
    checkpoint = boundary.capture_binding.checkpoint(point.receiver_identity, point.sequence + 5)
    scope = VerificationScope(
        pipeline_id=current['pipeline_id'], table_id=current['table_id'], copy_run_id=current['run_id'],
        source_library='TESTLIB', source_table='ITEMS', destination_database='DEST', destination_schema='PUBLIC',
        history_table='ITEMS_HISTORY', mirror_table='ITEMS_MIRROR',
        columns=(Column('ID', 'int'), Column('AMOUNT', 'decimal'), Column('LONG_BUSINESS_LABEL', 'text')),
        primary_key=('ID',), capture_binding=boundary.capture_binding, copy_boundary=point,
        checkpoint=checkpoint, receiver_windows=(ReceiverWindow(point.receiver_identity, point.sequence+1, checkpoint.sequence),),
        expected_snapshot_rows=0, producer_revision='a'*40, producer_version='0.4.5',
    )
    evidence = InitialCopyEvidence(current['pipeline_id'], current['table_id'], current['run_id'], boundary, 0,
                                   datetime(2026,10,16,8,3,tzinfo=timezone.utc), copy_observations=reference)
    raw = encode(evidence.to_dict())
    return store, scope, raw


def load(values, **changes):
    store, scope, raw = values
    return load_native_copy_inputs(store, raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                   evidence_key='proof', scope=scope, **changes)


def test_binary_bundle_contains_exact_descriptors_in_the_java_wire_order(native_inputs):
    result = load(native_inputs)
    stream = io.BytesIO(result.bundle)
    assert stream.read(8) == b'QDTNCB2\n'
    for phase in ('before', 'after'):
        expected = {value['id']: value for value in observation(phase)['originals']}
        assert struct.unpack('>iii', stream.read(12)) == (273, 273, 273)
        for key in ('file', 'format', 'rcac'):
            for field in ('receiver_base64', 'qualified_target_base64', 'error_base64'):
                length, = struct.unpack('>I', stream.read(4))
                assert stream.read(length) == base64.b64decode(expected[key][field], validate=True)
    assert stream.read(1) == b''
    assert result.bundle_sha256 == hashlib.sha256(result.bundle).hexdigest()
    assert result.member == 'ITEMS' and result.format_name == 'ITEMSFMT'
    assert result.jid_hex == '00112233445566778899'
    assert result.native_admission == 'not_evaluated'
    assert result.initial_context == encode(context())
    for phase, raw in zip(('before', 'after'), result.initial_observations, strict=True):
        assert raw == encode(observation(phase))


@pytest.mark.parametrize('change', [
    {'copy_run_id':'another'}, {'source_table':'OTHER'}, {'pipeline_id':'another'},
    {'expected_snapshot_rows':1}, {'columns':(Column('ID','int'), Column('AMOUNT','text'), Column('LONG_BUSINESS_LABEL','text'))},
])
def test_copy_inputs_cannot_be_rebound_to_another_scope(native_inputs, change):
    store, scope, raw = native_inputs
    with pytest.raises(ValueError):
        load((store, replace(scope, **change), raw))


def test_copy_frontier_and_initial_evidence_digest_are_not_replaceable(native_inputs):
    store, scope, raw = native_inputs
    with pytest.raises(ValueError):
        load_native_copy_inputs(store, raw, expected_sha256='f'*64, evidence_key='proof', scope=scope)
    point = scope.capture_binding.checkpoint(scope.copy_boundary.receiver_identity, scope.copy_boundary.sequence+1)
    changed = replace(scope, copy_boundary=point,
                      receiver_windows=(ReceiverWindow(point.receiver_identity,point.sequence+1,scope.checkpoint.sequence),))
    with pytest.raises(ValueError):
        load((store, changed, raw))


def test_v2_remains_readable_but_cannot_produce_native_inputs(native_inputs):
    import json
    store, scope, raw = native_inputs
    value = json.loads(raw)
    value.pop('copy_observations')
    value['format_version'] = 'quadringent-initial-copy-evidence-v2'
    assert InitialCopyEvidence.from_dict(value).copy_observations is None
    with pytest.raises(ValueError):
        load((store,scope,encode(value)))


def test_changed_or_missing_before_original_is_refused(native_inputs):
    import json
    store, scope, raw = native_inputs
    manifest = next(json.loads(value) for key,value in store.objects.items() if key.endswith('/manifest.json'))
    store.objects[manifest['before']['key']] += b' '
    with pytest.raises(ValueError):
        load((store,scope,raw))


def test_fixed_case_builds_only_the_scoped_java_invocation(native_inputs, tmp_path):
    _, scope, _ = native_inputs
    copy = load(native_inputs)
    bundle = tmp_path/'descriptor.bundle'
    bundle.write_bytes(copy.bundle)
    values = native_journal_processes(
        copy, scope, java='/fixture/java', classpath='/fixture/classes:/fixture/lib/*',
        credentials=b'host\nuser\nprivate-test-secret\n', environment={'AS400_TLS':'true'},
        bundle_path=bundle, guard_receipt_path=tmp_path/'guard.json', max_seconds=60,
    )
    assert len(values)==1 and values[0].window==scope.receiver_windows[0]
    process=values[0].process
    assert process.command[:4]==('/fixture/java','-cp','/fixture/classes:/fixture/lib/*',
                                 'io.quadringent.as400.NativeBusinessImageExport')
    args=process.command[4:]
    assert len(args)==27
    assert args[6:8]==('ITEMS','00112233445566778899')
    assert args[10]==scope.sha256 and args[11]==scope.copy_run_id
    assert args[20]==str(scope.copy_boundary.sequence)
    assert args[21:24]==(str(bundle),copy.bundle_sha256,str(tmp_path/'guard.json'))
    assert process.input_bytes.endswith(b'private-test-secret\n')
    assert 'private-test-secret' not in repr(process)
