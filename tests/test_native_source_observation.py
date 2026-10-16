"""Archived native bytes are reparsed offline; JSON claims cannot admit a source."""
from copy import deepcopy
from dataclasses import replace
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from quadringent.qualification.verification_source_observation import validate_source_observations
from quadringent.qualification.verification_contract import ReceiverWindow
from test_copy_observation import encode
from test_native_verification_native import native_inputs, load  # noqa: F401


def binary(raw):
    return base64.b64encode(raw).decode()


def reference(phase, document):
    raw = encode(document)
    return dict(phase=phase, metadata_base64=binary(raw), metadata_bytes=len(raw),
                metadata_sha256=hashlib.sha256(raw).hexdigest(), layout_sha256=document['format']['layout_sha256'])


def prefix(scope, document):
    identity = scope.checkpoint.receiver_identity
    codec = {37: 'cp037', 273: 'cp273', 367: 'ascii'}[document['originals'][0]['ccsid']]
    error = struct.pack('>i', 16) + bytes(12)
    raw = struct.pack('>ii', 58, 58) + bytes(0xf0 + int(c) for c in str(scope.checkpoint.sequence).zfill(20))
    raw += (identity.receiver_name.ljust(10) + identity.receiver_library.ljust(10) + '*SYSBAS'.ljust(10)).encode(codec)
    inputs = [identity.journal_name.ljust(10) + identity.journal_library.ljust(10)]
    values = [inputs[0].encode(codec), struct.pack('>iii', 1, 4, 1) + '2'.encode(codec), bytes(1),
              bytes(4), error, b'', struct.pack('>i', 58), 'SJNE0100'.encode(codec), bytes(4)]
    parameters = [dict(mode='INOUT' if i == 4 else 'OUT' if i == 5 else 'IN',
                       output_length=16 if i == 4 else 58 if i == 5 else 0,
                       input_base64=binary(v), input_sha256=hashlib.sha256(v).hexdigest())
                  for i, v in enumerate(values)]
    job = document['originals'][0]['job_before']
    return dict(journal_library=identity.journal_library, journal_name=identity.journal_name,
        receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
        sequence=str(scope.checkpoint.sequence), asp_device='*SYSBAS', program='QJOSJRNE', format='SJNE0100',
        force_key=4, force_value='2', entry_data_length=0, journal_type='*LOCAL', journal_state='*ACTIVE',
        sql_job=document['jdbc_job'], request_parameters=parameters, job_before=job, job_after=job,
        receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest(),
        error_base64=binary(error), error_sha256=hashlib.sha256(error).hexdigest(), transaction_cut=False)


@pytest.fixture
def archived_source(native_inputs):
    store, scope, evidence_bytes = deepcopy(native_inputs)
    evidence = json.loads(evidence_bytes)
    manifest_ref = evidence['copy_observations']
    manifest = json.loads(store.objects[manifest_ref['key']])
    for phase in ('before', 'after'):
        document = json.loads(store.objects[manifest[phase]['key']])
        for original in document['originals']:
            if original['id'] in {'jid-first', 'jid-last'}:
                raw = bytearray(base64.b64decode(original['receiver_base64']))
                raw[8:18] = scope.checkpoint.receiver_identity.journal_name.ljust(10).encode('cp273')
                original.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest())
        raw = encode(document)
        store.objects[manifest[phase]['key']] = raw
        manifest[phase].update(sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
    raw = encode(manifest)
    store.objects[manifest_ref['key']] = raw
    manifest_ref.update(sha256=hashlib.sha256(raw).hexdigest(), byte_count=len(raw))
    copy = load((store, scope, encode(evidence)))
    journal, final = guard_documents(copy, scope)
    return copy, scope, journal, final


def guard_documents(copy, scope):
    """Coherent offline originals for the actual prepared scope, with no source calls."""
    documents = []
    for phase, minute in [('before', '01'), ('after', '02'), ('before', '03')]:
        document = json.loads(copy.initial_observations[0])
        document['phase'] = phase
        document['witness_window']['start_sequence'] = '1'
        document['witness_window']['end_sequence'] = str(scope.checkpoint.sequence)
        document['started_at'] = f'2026-10-16T09:{minute}:00Z'
        document['completed_at'] = f'2026-10-16T09:{minute}:01Z'
        documents.append(document)
    pins = dict(scope_sha256=scope.sha256, context_sha256=copy.context_sha256,
        capture_binding_sha256=copy.binding_sha256, copy_boundary_sha256=copy.boundary_sha256,
        copy_run_id=scope.copy_run_id, bundle_sha256=copy.bundle_sha256)
    def receipt(mode, phases, selected):
        guards = []
        for phase, document in zip(phases, selected, strict=True):
            guards.append(dict(phase=phase, prefix=prefix(scope, document), entries=[], coverage=dict(
                receiver_library=scope.checkpoint.receiver_identity.receiver_library,
                receiver_name=scope.checkpoint.receiver_identity.receiver_name,
                start_sequence=str(scope.copy_boundary.sequence + 1), end_sequence=str(scope.checkpoint.sequence),
                journal_codes='D,F,R', state='complete', row_count=0, rows_sha256=hashlib.sha256(b'').hexdigest())))
        return dict(pins, format_version='quadringent-native-journal-guard-v1', mode=mode, state='complete',
                    copy_boundary=dict(receiver_library=scope.checkpoint.receiver_identity.receiver_library,
                        receiver_name=scope.checkpoint.receiver_identity.receiver_name, sequence=str(scope.copy_boundary.sequence)),
                    business_window=dict(receiver_library=scope.checkpoint.receiver_identity.receiver_library,
                        receiver_name=scope.checkpoint.receiver_identity.receiver_name,
                        start_sequence=str(scope.copy_boundary.sequence + 1), end_sequence=str(scope.checkpoint.sequence)),
                    copy_layout_sha256=json.loads(copy.initial_observations[0])['format']['layout_sha256'],
                    guards=guards, current_observations=[reference(p, d) for p, d in zip(phases, selected, strict=True)],
                    error_code=None, native_admission='not_evaluated', transaction_consistency_verified=False)
    return receipt('journal', ['before_business', 'after_business'], documents[:2]), receipt(
        'after_all_streams', ['after_all_streams'], documents[2:])


@pytest.fixture(scope='module')
def offline_java(tmp_path_factory):
    homebrew = Path('/opt/homebrew/opt/openjdk@21/bin')
    javac = str(homebrew / 'javac') if (homebrew / 'javac').is_file() else shutil.which('javac')
    jar = Path.home() / '.m2/repository/net/sf/jt400/jt400/21.0.7/jt400-21.0.7.jar'
    if not javac or not jar.is_file():
        pytest.skip('Cached JDK21/JTOpen required; no download')
    root = Path(__file__).resolve().parents[1]
    package = root / 'java/src/main/java/io/quadringent/as400'
    names = ['TlsTrust', 'NativeVerificationSourceExport', 'ReadOnlyFormatIdentity', 'ReadOnlyMemberIdentity',
             'NativeBusinessImageExport', 'NativeCopyObservationExport', 'NativeCopyBundle',
             'NativeBusinessImageProducer', 'NativeJournalPrefixForce', 'NativeLifecycleGuard',
             'NativeSourceObservationVerify']
    classes = tmp_path_factory.mktemp('native-source-offline')
    compiled = subprocess.run([javac, '--release', '21', '-cp', str(jar), '-d', str(classes),
                    *(str(package / (n + '.java')) for n in names)], capture_output=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr.decode()
    strict = subprocess.run([javac, '--release', '21', '-Xlint:all', '-Werror', '-cp',
        str(classes) + os.pathsep + str(jar), '-d', str(classes), str(package / (names[-1] + '.java'))],
        capture_output=True, timeout=60)
    assert strict.returncode == 0, strict.stderr.decode()
    return dict(java=str(Path(javac).with_name('java')), classpath=str(classes) + os.pathsep + str(jar),
                environment={'LANG': 'C', 'TZ': 'UTC'})


def verify(value, java, directory, **changes):
    copy, scope, journal, final = value
    return validate_source_observations(copy, scope, encode(journal), encode(final), directory=directory,
                                        check_current=lambda: True, **java, **changes)


def test_original_native_bytes_are_reparsed_without_source_connection(archived_source, offline_java, tmp_path):
    before = archived_source[0].initial_context, archived_source[0].initial_observations
    result = verify(archived_source, offline_java, tmp_path / 'verify')
    assert result['native_admission'] == 'not_evaluated'
    assert result['transaction_consistency_verified'] is False
    assert result['validated_current_observations'] == 3
    assert result['scope_sha256'] == archived_source[1].sha256
    assert (archived_source[0].initial_context, archived_source[0].initial_observations) == before


def test_relabelled_scope_cannot_move_original_copy_frontier(archived_source, offline_java, tmp_path):
    copy, scope, journal, final = deepcopy(archived_source)
    point = scope.capture_binding.checkpoint(scope.copy_boundary.receiver_identity, scope.copy_boundary.sequence + 1)
    moved = replace(scope, copy_boundary=point, receiver_windows=(ReceiverWindow(
        point.receiver_identity, point.sequence + 1, scope.checkpoint.sequence),))
    for receipt in (journal, final):
        receipt['scope_sha256'] = moved.sha256
        receipt['copy_boundary']['sequence'] = str(point.sequence)
        receipt['business_window']['start_sequence'] = str(point.sequence + 1)
        for guard in receipt['guards']:
            guard['coverage']['start_sequence'] = str(point.sequence + 1)
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify((replace(copy, scope_sha256=moved.sha256), moved, journal, final), offline_java, tmp_path / 'verify')


@pytest.mark.parametrize('fault', ['missing', 'base64', 'hash', 'size', 'member', 'jid', 'format', 'window',
                                 'job', 'jdbc_job', 'typed_layout', 'lifecycle', 'prefix', 'claim'])
def test_lying_metadata_or_rehashed_foreign_native_bytes_are_refused(archived_source, offline_java, tmp_path, fault):
    copy, scope, journal, final = deepcopy(archived_source)
    if fault == 'missing':
        journal.pop('current_observations')
    elif fault in {'base64', 'hash', 'size'}:
        journal['current_observations'][0][{'base64': 'metadata_base64', 'hash': 'metadata_sha256', 'size': 'metadata_bytes'}[fault]] = (
            '!' if fault == 'base64' else 'f' * 64 if fault == 'hash' else 1)
    elif fault == 'lifecycle':
        journal['guards'][0]['entries'] = [{'journal_code': 'D', 'journal_entry_type': 'DL'}]
    elif fault == 'prefix':
        journal['guards'][0]['prefix']['sequence'] = str(scope.checkpoint.sequence + 1)
    elif fault == 'claim':
        journal['native_admission'] = 'verified'
    else:
        index = 1 if fault == 'jdbc_job' else 0
        document = json.loads(base64.b64decode(journal['current_observations'][index]['metadata_base64']))
        if fault in {'member', 'jid', 'format'}:
            oid = {'member': 'member-before', 'jid': 'jid-first', 'format': 'format'}[fault]
            original = next(o for o in document['originals'] if o['id'] == oid)
            raw = bytearray(base64.b64decode(original['receiver_base64']))
            raw[{'member': 28, 'jid': 28, 'format': 70}[fault]] ^= 1
            original.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest())
            if fault == 'format':
                parts = {o['id']: o for o in document['originals']}
                data = b''.join(base64.b64decode(parts[n][k + '_base64']) for n in ('file', 'format', 'rcac')
                                for k in ('receiver', 'qualified_target', 'error'))
                data += b''.join(struct.pack('>i', parts[n]['ccsid']) for n in ('file', 'format', 'rcac'))
                document['format']['layout_sha256'] = hashlib.sha256(data).hexdigest()
        elif fault == 'window':
            document['witness_window']['end_sequence'] = str(scope.checkpoint.sequence + 1)
        elif fault == 'job':
            document['originals'][1]['job_before']['number'] = '999999'
            document['originals'][1]['job_after']['number'] = '999999'
        elif fault == 'jdbc_job':
            document['jdbc_job']['number'] = '999999'
        else:
            document['format']['fields'][0]['digits'] = 9
        journal['current_observations'][index] = reference(journal['current_observations'][index]['phase'], document)
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify((copy, scope, journal, final), offline_java, tmp_path / 'verify')


def entry(copy, scope, *, code='R', kind='PT'):
    data = bytes(32)
    identity = scope.checkpoint.receiver_identity
    return dict(receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
        sequence=str(scope.copy_boundary.sequence + 1), journal_code=code, journal_entry_type=kind,
        object=scope.source_table.ljust(10) + scope.source_library.ljust(10) + copy.member.ljust(10),
        object_type='*FILE', jid_hex=copy.jid_hex.upper(), minimized_entry_data='0', minimized_on_field_boundary='0',
        indicator_flag='1', null_value_indicators='000', declared_payload_bytes=len(data), entry_data_bytes=len(data),
        entry_data_base64=binary(data), entry_data_sha256=hashlib.sha256(data).hexdigest())


def recorded_entries(guard, values):
    guard['entries'] = values
    guard['coverage']['row_count'] = len(values)
    guard['coverage']['rows_sha256'] = hashlib.sha256(b''.join(
        (json.dumps(v, separators=(',', ':'), ensure_ascii=False) + '\n').encode() for v in values)).hexdigest()


@pytest.mark.parametrize('code,kind', [('R', 'PT'), ('R', 'PX'), ('R', 'UB'), ('R', 'UP'), ('R', 'DL'), ('F', 'OP'), ('F', 'CL')])
def test_supported_complete_native_entry_shapes_preserve_not_evaluated(archived_source, offline_java, tmp_path, code, kind):
    copy, scope, journal, _ = archived_source
    recorded_entries(journal['guards'][0], [entry(copy, scope, code=code, kind=kind)])
    assert verify(archived_source, offline_java, tmp_path / 'verify')['native_admission'] == 'not_evaluated'


@pytest.mark.parametrize('fault', ['delete-file', 'change-file', 'unknown-record', 'foreign-member', 'foreign-jid',
                                 'minimized', 'nulls', 'delete-indicator', 'sequence', 'short-payload', 'hash'])
def test_rehashed_complete_lifecycle_rows_are_semantically_checked(archived_source, offline_java, tmp_path, fault):
    copy, scope, journal, _ = archived_source
    row = entry(copy, scope)
    if fault == 'delete-file':
        row.update(journal_code='D', journal_entry_type='DL')
    elif fault == 'change-file':
        row.update(journal_code='F', journal_entry_type='CR')
    elif fault == 'unknown-record':
        row['journal_entry_type'] = 'UR'
    elif fault == 'foreign-member':
        row['object'] = row['object'][:20] + 'OTHER'.ljust(10)
    elif fault == 'foreign-jid':
        row['jid_hex'] = 'f' * 20
    elif fault == 'minimized':
        row['minimized_entry_data'] = '1'
    elif fault == 'nulls':
        row['null_value_indicators'] = '001'
    elif fault == 'delete-indicator':
        row.update(journal_entry_type='DL', indicator_flag='0')
    elif fault == 'sequence':
        row['sequence'] = str(scope.copy_boundary.sequence)
    elif fault == 'short-payload':
        row.update(declared_payload_bytes=1, entry_data_bytes=1, entry_data_base64=binary(b'x'),
                   entry_data_sha256=hashlib.sha256(b'x').hexdigest())
    else:
        row['entry_data_sha256'] = '0' * 64
    recorded_entries(journal['guards'][0], [row])
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify(archived_source, offline_java, tmp_path / 'verify')


def test_later_guard_can_use_another_native_and_jdbc_session(archived_source, offline_java, tmp_path):
    _, _, _, final = archived_source
    document = json.loads(base64.b64decode(final['current_observations'][0]['metadata_base64']))
    document['jdbc_job']['number'] = '999998'
    for original in document['originals']:
        original['job_before']['number'] = original['job_after']['number'] = '999999'
    final['current_observations'][0] = reference('after_all_streams', document)
    final['guards'][0]['prefix'] = prefix(archived_source[1], document)
    assert verify(archived_source, offline_java, tmp_path / 'verify')['validated_guards'] == 3


@pytest.mark.parametrize('ccsid,codec', [(37, 'cp037'), (367, 'ascii')])
def test_supported_job_ccsid_changes_preserve_the_same_decoded_member(archived_source, offline_java, tmp_path, ccsid, codec):
    from test_copy_observation import update_layout_hash
    _, scope, _, final = archived_source
    document = json.loads(base64.b64decode(final['current_observations'][0]['metadata_base64']))
    for original in document['originals']:
        raw = bytearray(base64.b64decode(original['receiver_base64']))
        spans = []
        if original['id'] == 'format':
            spans = [(70, 10), (80, 13)] + [(256 + i * 112 + offset, 30) for i in range(3) for offset in (4, 34)]
        elif original['id'] == 'rcac':
            spans = [(21, 2)]
        elif original['id'].startswith('member-'):
            extra = int.from_bytes(raw[244:248], 'big')
            spans = [(i, 10) for i in (8, 18, 28, 38)] + [(58, 13), (186, 13)]
            spans += [(i, 1) for i in (134, 135, 136, extra + 124, extra + 125)]
        elif original['id'].startswith('jid-'):
            spans = [(i, 10) for i in (8, 18, 38, 48, 58, 68)] + [(78, 16)]
        for offset, size in spans:
            raw[offset:offset+size] = raw[offset:offset+size].decode('cp273').encode(codec)
        original.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest(), ccsid=ccsid)
        original['job_before']['ccsid'] = original['job_after']['ccsid'] = ccsid
        if original['qualified_target_base64'] is not None:
            target = base64.b64decode(original['qualified_target_base64']).decode('cp273').encode(codec)
            original.update(qualified_target_base64=binary(target), qualified_target_sha256=hashlib.sha256(target).hexdigest())
    update_layout_hash(document)
    final['current_observations'][0] = reference('after_all_streams', document)
    final['guards'][0]['prefix'] = prefix(scope, document)
    assert verify(archived_source, offline_java, tmp_path / 'verify')['native_admission'] == 'not_evaluated'


def test_rehashed_native_creation_change_is_not_hidden_by_typed_member(archived_source, offline_java, tmp_path):
    _, _, _, final = archived_source
    document = json.loads(base64.b64decode(final['current_observations'][0]['metadata_base64']))
    for original in document['originals'][3::3]:
        raw = bytearray(base64.b64decode(original['receiver_base64']))
        raw[70] ^= 1
        original.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest())
    final['current_observations'][0] = reference('after_all_streams', document)
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify(archived_source, offline_java, tmp_path / 'verify')


def test_consistent_typed_layout_lie_in_initial_and_current_originals_is_refused(archived_source, offline_java, tmp_path):
    copy, scope, journal, final = archived_source
    originals = []
    for raw in copy.initial_observations:
        document = json.loads(raw)
        document['format']['fields'][0]['digits'] = 9
        originals.append(encode(document))
    for receipt in (journal, final):
        for index, item in enumerate(receipt['current_observations']):
            document = json.loads(base64.b64decode(item['metadata_base64']))
            document['format']['fields'][0]['digits'] = 9
            receipt['current_observations'][index] = reference(item['phase'], document)
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify((replace(copy, initial_observations=tuple(originals)), scope, journal, final), offline_java, tmp_path / 'verify')


@pytest.mark.parametrize('fence', [False, None, 1])
def test_nontrue_fence_refuses_before_creating_files(archived_source, offline_java, tmp_path, fence):
    copy, scope, journal, final = archived_source
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        validate_source_observations(copy, scope, encode(journal), encode(final), directory=tmp_path / 'verify',
            check_current=lambda: fence, **offline_java)
    assert not (tmp_path / 'verify').exists()


def test_oversize_original_refuses_before_process_or_files(archived_source, offline_java, tmp_path):
    copy, scope, journal, final = archived_source
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        validate_source_observations(copy, scope, encode(journal) + bytes(4 * 1024**2), encode(final),
            directory=tmp_path / 'verify', check_current=lambda: True, **offline_java)
    assert not (tmp_path / 'verify').exists()


def test_actual_failing_offline_process_cleans_up_and_never_exposes_output(archived_source, offline_java, tmp_path):
    import sys
    executable = tmp_path / 'offline'
    executable.write_text('#!' + sys.executable + '\nimport sys\nprint("PRIVATE_OUTPUT_CANARY", file=sys.stderr)\n'
                          'print("{}")\nsys.exit(2)\n')
    executable.chmod(0o700)
    with pytest.raises(ValueError, match='^native_source_observations_failed$') as failure:
        verify(archived_source, dict(offline_java, java=str(executable)), tmp_path / 'verify')
    assert 'PRIVATE_OUTPUT_CANARY' not in str(failure.value)
    assert not (tmp_path / 'verify').exists()


def test_context_fence_changed_after_actual_process_prevents_a_result(archived_source, offline_java, tmp_path):
    copy, scope, journal, final = archived_source
    directory = tmp_path / 'verify'
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        validate_source_observations(copy, scope, encode(journal), encode(final), directory=directory,
            check_current=lambda: not (directory / 'offline' / 'original.json').exists(), **offline_java)
    assert not directory.exists()


def test_cleanup_failure_cannot_report_a_successful_revalidation(archived_source, offline_java, tmp_path, monkeypatch):
    import quadringent.qualification.verification_source_observation as module
    def fail_cleanup(*args, **kwargs):
        raise OSError('PRIVATE_CLEANUP_CANARY')
    monkeypatch.setattr(module.shutil, 'rmtree', fail_cleanup)
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify(archived_source, offline_java, tmp_path / 'verify')


def test_slow_context_callback_is_charged_to_the_global_deadline(archived_source, offline_java, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import quadringent.qualification.verification_guard as guard
    clock = [0.0]
    monkeypatch.setattr(guard, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def current():
        clock[0] += 2
        return True
    copy, scope, journal, final = archived_source
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        validate_source_observations(copy, scope, encode(journal), encode(final), directory=tmp_path / 'verify',
            check_current=current, max_seconds=1, **offline_java)
    assert not (tmp_path / 'verify').exists()


@pytest.mark.parametrize('fault', ['missing-initial', 'foreign-run', 'foreign-source', 'native-journal', 'prefix-raw',
                                 'prefix-parameters', 'prefix-error', 'duplicate-field'])
def test_original_scope_and_native_prefix_cannot_be_relabelled(archived_source, offline_java, tmp_path, fault):
    copy, scope, journal, final = archived_source
    if fault == 'missing-initial':
        copy = replace(copy, initial_observations=(b'', b''))
    elif fault in {'foreign-run', 'foreign-source'}:
        scope = replace(scope, **({'copy_run_id': 'foreign'} if fault == 'foreign-run' else {'source_table': 'OTHER'}))
        copy = replace(copy, scope_sha256=scope.sha256)
        for receipt in (journal, final):
            receipt.update(scope_sha256=scope.sha256, copy_run_id=scope.copy_run_id)
    elif fault == 'native-journal':
        document = json.loads(base64.b64decode(journal['current_observations'][0]['metadata_base64']))
        for original in document['originals'][4:6]:
            raw = bytearray(base64.b64decode(original['receiver_base64']))
            raw[8:18] = 'FOREIGN'.ljust(10).encode('cp273')
            original.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest())
        journal['current_observations'][0] = reference('before_business', document)
    elif fault == 'prefix-raw':
        prefix = journal['guards'][0]['prefix']
        raw = bytearray(base64.b64decode(prefix['receiver_base64']))
        raw[28:38] = 'FOREIGN'.ljust(10).encode('cp273')
        prefix.update(receiver_base64=binary(raw), receiver_sha256=hashlib.sha256(raw).hexdigest())
    elif fault == 'prefix-parameters':
        data = b'WRONG_TARGET'
        journal['guards'][0]['prefix']['request_parameters'][0].update(
            input_base64=binary(data), input_sha256=hashlib.sha256(data).hexdigest())
    elif fault == 'prefix-error':
        data = struct.pack('>ii', 16, 1) + bytes(8)
        journal['guards'][0]['prefix'].update(error_base64=binary(data), error_sha256=hashlib.sha256(data).hexdigest())
    else:
        raw = encode(journal).replace(b'"mode":"journal"', b'"mode":"journal","mode":"journal"')
        with pytest.raises(ValueError, match='^native_source_observations_failed$'):
            validate_source_observations(copy, scope, raw, encode(final), directory=tmp_path / 'verify',
                                        check_current=lambda: True, **offline_java)
        return
    with pytest.raises(ValueError, match='^native_source_observations_failed$'):
        verify((copy, scope, journal, final), offline_java, tmp_path / 'verify')
