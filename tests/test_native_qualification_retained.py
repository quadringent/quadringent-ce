"""Oracles multi-époques : aucune ligne Snow ne devient un oracle attendu."""
from argparse import Namespace
import base64
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.raw import RawBatchWriter
from quadringent.qualification import cli, collect, proof
import test_native_qualification_collect as fixtures


def fixture(run='old'):
    copy, _, _, _, args, columns = fixtures.OracleCollectorTests().fixture()
    args.copy_run_id = run
    copy['run_id'] = run
    event = ChangeEvent('ibmi', 'SNAPSHOT:S.T', 'S', 'T', 'c', JournalPosition('SNAPSHOT:' + run, 1),
                        '2026-10-04T20:00:00Z', 'schema', None, {'ID': 1, 'NOTE': 'old'})
    copy['records'] = [event.to_record()]
    copy['artifacts'] = [{'evidence_key': f'raw/t/evidence/{run}.json', 'verified': True, 'sha256': 'a' * 64}]
    journal = {'receiver': 'R1', 'start': 11, 'end': 10, 'decoded': 0, 'scan_complete': True, 'records': [], 'raw_batches': []}
    positions = {'kind': 'ibmi-source-rowpos-reader', 'positions': [],
                 'rowpos_b64': base64.b64encode(b'SRC_ROWPOS_COUNT=0\n').decode()}
    checkpoint = {'receiver': 'R1', 'sequence': 10}
    args.max_seconds=120; args.poll_seconds=1
    args.pk = 'ID'
    args.capture_digest = 'sha256:' + 'b' * 64
    parts = dict(copy=copy, journal=journal, positions=positions, checkpoint=checkpoint)
    oracle = collect.validate_retained_parts(parts, args, columns)
    return parts, args, columns, oracle


def job(args, key):
    return {'metadata': {'namespace':args.namespace,'uid': 'job-' + args.copy_run_id, 'name': 'copy-' + args.copy_run_id,
                         'creationTimestamp': '2026-10-04T20:00:00Z',
                         'labels': {'quadringent.io/table-id': args.table_id, 'quadringent.io/pipeline-id': args.pipeline,
                                    'quadringent.io/component': 'initial-copy'},
                         'annotations': {'quadringent.io/run-id': args.copy_run_id,
                                         'quadringent.io/bootstrap-boundary': args.copy_boundary}},
            'spec': {'template': {'spec': {'containers': [{'name': 'initial-copy', 'image': 'runtime@' + args.capture_digest,
                     'env': [{'name': 'AS400_EVIDENCE_KEY', 'value': key}, {'name': 'AS400_PIPELINE_ID', 'value': args.pipeline}]}]}}},
            'status': {'succeeded': 1, 'conditions': [{'type': 'Complete', 'status': 'True'}]}}


def test_two_snapshot_epochs_preserve_history_and_require_exhaustive_equality():
    *_, old = fixture('old')
    *_, new = fixture('new')
    union = collect.union_history_oracles(new, [old])
    assert len(union['events']) == 2
    assert union['copy_run_id'] == 'new'
    assert union['native_cdc_qualified'] is False
    assert proof.history_proof(union['events'], union['events'])['equal']
    with pytest.raises(ValueError):
        proof.history_proof(union['events'], new['events'])  # Omitting old epoch cannot pass.
    with pytest.raises(ValueError):
        proof.history_proof(old['events'], union['events'])  # Omitting new epoch cannot pass.


@pytest.mark.parametrize('change', ['scope', 'same-run', 'duplicate-epoch', 'foreign-kind', 'non-independent', 'duplicate-event', 'position-conflict'])
def test_union_refuses_foreign_duplicate_or_contradictory_epochs(change):
    *_, old = fixture('old')
    *_, new = fixture('new')
    retained = [old]
    if change == 'scope': old['pipeline'] = 'foreign'
    elif change == 'same-run': old['copy_run_id'] = 'new'
    elif change == 'duplicate-epoch': retained.append(deepcopy(old))
    elif change == 'foreign-kind': old['kind'] = 'snow-derived'
    elif change == 'non-independent': old['independent_of_snowflake'] = False
    elif change == 'duplicate-event': old['events'].append(deepcopy(old['events'][0]))
    elif change == 'position-conflict':
        old['events'][0]['JOURNAL_RECEIVER'] = new['events'][0]['JOURNAL_RECEIVER']
    with pytest.raises(ValueError): collect.union_history_oracles(new, retained)


@pytest.mark.parametrize('change', ['digest', 'run', 'table', 'boundary', 'evidence-key', 'failed', 'incomplete', 'uid'])
def test_real_job_binding_rejects_wrong_image_scope_run_or_completion(change):
    parts, args, _, _ = fixture()
    key = parts['copy']['artifacts'][0]['evidence_key']
    actual = job(args, key)
    assert collect.initial_copy_job(actual, args, key)['image'].endswith(args.capture_digest)
    container = actual['spec']['template']['spec']['containers'][0]
    if change == 'digest': container['image'] = 'runtime@sha256:' + 'c' * 64
    elif change == 'run': actual['metadata']['annotations']['quadringent.io/run-id'] = 'other'
    elif change == 'table': actual['metadata']['labels']['quadringent.io/table-id'] = 'other'
    elif change == 'boundary': actual['metadata']['annotations']['quadringent.io/bootstrap-boundary'] = 'R1:9'
    elif change == 'evidence-key': container['env'][0]['value'] = 'other.json'
    elif change == 'failed': actual['status']['failed'] = 1
    elif change == 'incomplete': actual['status']['conditions'] = []
    elif change == 'uid': actual['metadata'].pop('uid')
    with pytest.raises(ValueError): collect.initial_copy_job(actual, args, key)


def test_archived_journal_bytes_and_rowpos_are_redecoded(tmp_path):
    parts, args, columns, _ = fixture()
    event = ChangeEvent('ibmi', 'J', 'S', 'T', 'c', JournalPosition('R1', 11),
                        '2026-10-04T20:01:00Z', 'schema', None, {'ID': 2, 'NOTE': 'new'})
    RawBatchWriter(tmp_path).write_batch([event], high_watermark=event.position)
    manifest = next(tmp_path.glob('*.manifest.json'))
    payload = Path(str(manifest).replace('.manifest.json', '.jsonl'))
    parts['journal'].update(end=11, decoded=1, records=[event.to_record()], raw_batches=[{
        'manifest_b64': base64.b64encode(manifest.read_bytes()).decode(),
        'payload_b64': base64.b64encode(payload.read_bytes()).decode()}])
    parts['positions']['positions'] = [{'JOURNAL_RECEIVER_NAME': 'R1', 'SEQUENCE_NUMBER': 11}]
    parts['positions']['rowpos_b64'] = base64.b64encode(('SRC_ROWPOS='+json.dumps(parts['positions']['positions'][0])+'\nSRC_ROWPOS_COUNT=1\n').encode()).decode()
    parts['checkpoint']['sequence'] = 11
    assert len(collect.validate_retained_parts(parts, args, columns)['events']) == 2
    for change in ('payload', 'records', 'rowpos', 'no-bytes'):
        broken = deepcopy(parts)
        if change == 'payload': broken['journal']['raw_batches'][0]['payload_b64'] = base64.b64encode(b'tampered').decode()
        elif change == 'records': broken['journal']['records'][0]['after']['NOTE'] = 'tampered'
        elif change == 'rowpos': broken['positions']['positions'] = []
        else: broken['journal']['raw_batches'] = []
        with pytest.raises(ValueError): collect.validate_retained_parts(broken, args, columns)


def private(path, data):
    path.write_text(json.dumps(data)); path.chmod(0o600)
    return {'path': str(path), 'sha256': cli.sha(path)}


def validation_fixture(tmp_path):
    parts, old_args, columns, oracle = fixture('old')
    _, current_args, _, current_oracle = fixture('new')
    config = {'native': {}, 'release': {'mode': 'conserved'}}
    config_ref = private(tmp_path / 'old-config.json', config)
    old_job = job(old_args, parts['copy']['artifacts'][0]['evidence_key'])
    current_job = job(current_args, 'raw/t/evidence/new.json')
    bundle = {'kind': 'quadringent-retained-epoch-v1', 'parts': parts,
              'job': collect.initial_copy_job(old_job, old_args, parts['copy']['artifacts'][0]['evidence_key']),
              'oracle': oracle, 'snapshot': {}, 'observed_utc': '2026-10-04T20:01:00Z'}
    bundle_ref = private(tmp_path / 'old-bundle.json', bundle)
    manifest = {'kind': 'quadringent-retained-history-v1', 'scope': {k: getattr(current_args, k) for k in collect.SCOPE},
                'epochs': [{'config': config_ref, 'bundle': bundle_ref}], 'known_copy_runs': ['old'],
                'observed_utc': '2026-10-04T20:02:00Z', 'native_cdc_qualified': False}
    probe = Mock(args=current_args, columns=columns)
    collector = Mock(); collector.copy.return_value = parts['copy']
    return manifest, probe, collector, [old_job, current_job], config, old_args, bundle, current_oracle


@pytest.mark.parametrize('change', ['none', 'omitted-run', 'bundle-bytes', 'config-bytes', 'object-bytes', 'job', 'foreign-scope', 'admission'])
def test_retained_revalidation_checks_pins_objects_inventory_job_and_old_admission(tmp_path, change):
    manifest, probe, collector, jobs, config, old_args, bundle, _ = validation_fixture(tmp_path)
    entry = manifest['epochs'][0]
    if change == 'omitted-run': manifest['known_copy_runs'] = []
    elif change == 'bundle-bytes': Path(entry['bundle']['path']).write_text('{}')
    elif change == 'config-bytes': Path(entry['config']['path']).write_text('{}')
    elif change == 'object-bytes': collector.copy.return_value = {**bundle['parts']['copy'], 'rows_copied': 2}
    elif change == 'job': jobs[0]['metadata']['uid'] = 'other-job'
    elif change == 'foreign-scope': old_args.pipeline = 'foreign'
    old_probe = Mock(args=old_args, columns=probe.columns)
    with patch.object(cli, 'load_site', return_value=config), patch.object(cli, 'release_proof', return_value='admitted') as release, \
            patch.object(cli, 'native_arguments', return_value=old_args), patch.object(proof, 'NativeProbe', return_value=old_probe), \
            patch.object(cli, 'validate_retained_snapshot'):
        if change == 'admission': release.side_effect = ValueError('old admission invalid')
        if change == 'none':
            assert cli.revalidate_retained(manifest, probe, collector, None, None, jobs)[0]['copy_run_id'] == 'old'
            release.assert_called_once()  # An old epoch cannot inherit the current release admission.
        else:
            with pytest.raises(ValueError): cli.revalidate_retained(manifest, probe, collector, None, None, jobs)


def test_no_manifest_refuses_prior_jobs_and_does_not_fall_back_to_single_epoch():
    _, args, _, oracle = fixture('new')
    _, old_args, _, _ = fixture('old')
    probe = Mock(args=args)
    probe.kubectl.return_value = {'items': [job(args, 'new.json'), job(old_args, 'old.json')]}
    collector = Mock(probe=probe); collector.oracle.return_value = {**oracle, 'reader': {}, 'loader': {}}
    with pytest.raises(ValueError): cli.retained_oracle(collector, None)


def test_manifest_pin_and_private_mode_are_required(tmp_path):
    path = tmp_path / 'manifest.json'
    private(path, {'kind': 'quadringent-retained-history-v1'})
    with pytest.raises(ValueError): cli.retained_manifest({'retained_history_manifest': str(path)})
    with pytest.raises(ValueError): cli.retained_manifest({'retained_history_manifest': str(path), 'retained_history_sha256': '0' * 64})
    ref = {'path': str(path), 'sha256': cli.sha(path)}
    path.chmod(0o644)
    with pytest.raises(ValueError): cli.pinned_document(ref)


def snapshot_fixture():
    parts, args, columns, oracle = fixture('old')
    args.identity = 'vm-metadata'; args.expected_role_arn = 'arn:aws:iam::000000000000:role/test-role'
    args.loader_digest = 'sha256:' + 'c' * 64
    mappings = {}
    snapshot = {k: getattr(args, k) for k in collect.SCOPE}
    snapshot.update(snapshot_checks_passed=True, copy_job_observed_succeeded=True, duplicate_event_ids=0,
                    history_mode='streaming', checkpoint=parts['checkpoint'], reconciliation={'equal': True},
                    history=oracle['events'], source=[{'ID': 1, 'NOTE': 'old'}], mirror=[{'ID': 1, 'NOTE': 'old'}])
    for key, component, digest in (('reader', 'capture', args.capture_digest), ('loader', 'control-plane', args.loader_digest)):
        mappings[component] = {'index': digest, 'revision': 'a' * 40, 'receipt_sha256': 'e' * 64,
                               'admission_sha256': 'f' * 64,
                               'variants': {'amd64': {'manifest': 'sha256:' + 'd' * 64, 'config': 'sha256:' + 'e' * 64}}}
        image_id = 'containerd://sha256:' + 'd' * 64
        snapshot[key] = {'container': key, 'images': [{'name': key, 'image_id': image_id}],
                         'image_binding': proof.image_binding(mappings[component], digest, image_id, 'amd64'),
                         'identity_mode': args.identity, 'service_account': key,
                         'identity_effective': {'method': 'iam-role', 'verified': True,
                                                'arn': 'arn:aws:sts::000000000000:assumed-role/test-role/session'}}
    return snapshot, oracle, Mock(args=args, columns=columns, release_images=mappings)


@pytest.mark.parametrize('change', ['none', 'image-admission', 'image-id', 'role', 'history-omission', 'mirror', 'isolated-pass-bool'])
def test_preserved_baseline_requires_old_image_admission_full_history_and_source_equality(change):
    snapshot, oracle, probe = snapshot_fixture()
    if change == 'image-admission': probe.release_images['capture']['revision'] = 'b' * 40
    elif change == 'image-id': snapshot['reader']['images'][0]['image_id'] = 'containerd://sha256:' + '9' * 64
    elif change == 'role': snapshot['reader']['identity_effective']['arn'] = 'arn:aws:sts::000000000000:assumed-role/foreign/session'
    elif change == 'history-omission': snapshot['history'] = []
    elif change == 'mirror': snapshot['mirror'][0]['NOTE'] = 'changed'
    elif change == 'isolated-pass-bool': snapshot['copy_job_observed_succeeded'] = False
    if change == 'none': cli.validate_retained_snapshot(snapshot, oracle, probe)
    else:
        with pytest.raises(ValueError): cli.validate_retained_snapshot(snapshot, oracle, probe)


def test_cli_capture_records_real_parts_job_and_old_config_pins(tmp_path):
    from io import StringIO
    snapshot, oracle, probe = snapshot_fixture()
    parts, args, _, _ = fixture('old')
    probe.args = args
    probe.snapshot.return_value = snapshot
    data = {'native': {}, 'release': {'mode': 'conserved', 'metadata_root': 'metadata', 'source_sha': 'a'*40}}
    config = tmp_path / 'config.json'; private(config, data)
    current = {**oracle, 'retained_parts': parts}
    jobs = [job(args, parts['copy']['artifacts'][0]['evidence_key'])]
    jobs[0].update(apiVersion='batch/v1',kind='Job')
    jobs[0]['metadata']['namespace']=args.namespace
    jobs[0]['status']['completionTime']='2026-10-04T20:01:00Z'
    jobs[0]['spec']['ttlSecondsAfterFinished']=3600
    probe.initial_copy_job_api.return_value=jobs[0]
    output = StringIO()
    with patch.object(cli, 'load_site', return_value=data), patch.object(cli, 'release_proof', return_value='old-admission'), \
            patch.object(collect, 'config_values', return_value=args), patch.object(proof, 'NativeProbe', return_value=probe), \
            patch.object(cli, 'retained_oracle', return_value=(deepcopy(current), current, jobs)), \
            patch.object(cli, 'validate_retained_snapshot'), patch.object(proof,'utc',return_value='2026-10-04T20:02:00Z'):
        assert cli.run_native(str(config), str(tmp_path / 'out'), 'retain-history', stdout=output) == 0
    manifest_path = tmp_path / 'out' / 'retain-history.json'
    manifest = cli.private_json(manifest_path)
    assert manifest['known_copy_runs'] == ['old']
    assert manifest['native_cdc_qualified'] is False
    epoch = manifest['epochs'][0]
    assert cli.pinned_document(epoch['config']) == data
    bundle = cli.pinned_document(epoch['bundle'])
    assert bundle['parts'] == parts
    assert bundle['job']['run_id'] == 'old'
    assert bundle['oracle']['copy_run_id'] == 'old'
    assert bundle['snapshot'] == snapshot
    assert manifest_path.stat().st_mode & 0o777 == 0o600


def test_native_probe_separates_old_copy_admission_from_current_workloads(tmp_path, monkeypatch):
    _, args, _, _ = fixture('old')
    current = {'capture': {'index': args.capture_digest}, 'control-plane': {'index': 'sha256:' + 'c' * 64}}
    old = {'capture': {'index': 'sha256:' + '9' * 64}}
    columns_ref = private(tmp_path / 'columns.json', {'ID': 'int', 'NOTE': 'text'})
    vars(args).update(columns=columns_ref['path'], database='DB', schema='S', history='H', mirror='M',
                      reader_deployment='reader', loader_deployment='loader', loader_digest=current['control-plane']['index'],
                      release_oci_root='current', release_receipt='current/receipt', release_admission='current/admission',
                      expected_revision='a'*40, initial_copy_oci_root='old', initial_copy_receipt='old/receipt',
                      initial_copy_revision='b'*40, initial_copy_admission='old/admission', initial_copy_capture_digest=old['capture']['index'])
    monkeypatch.setattr(proof, 'release_bindings', lambda root, *_: current if root == 'current' else old)
    probe = proof.NativeProbe(args)
    assert probe.release_images == current
    assert probe.copy_capture_digest == old['capture']['index']
    assert probe.args.capture_digest == current['capture']['index']
    key = 'raw/t/evidence/old.json'
    actual = job(args, key)
    # A current-runtime image is not the admitted historical Job image.
    with pytest.raises(ValueError): collect.initial_copy_job(actual, args, key)
    actual['spec']['template']['spec']['containers'][0]['image'] = 'runtime@' + old['capture']['index']
    assert collect.initial_copy_job(actual, args, key)['run_id'] == 'old'
    args.initial_copy_capture_digest = 'sha256:' + '8' * 64
    with pytest.raises(ValueError): proof.NativeProbe(args)
    args.initial_copy_capture_digest = old['capture']['index']
    args.loader_digest = old['capture']['index']
    with pytest.raises(ValueError): proof.NativeProbe(args)  # Runtime cannot inherit the older admission.


def test_native_arguments_requires_crypto_initial_copy_admission_not_a_free_digest():
    _, args, _, _ = fixture()
    data = {'native': {}, 'release': {'metadata_root': 'current', 'source_sha': 'a'*40},
            'initial_copy_release': {'metadata_root': 'old', 'source_sha': 'b'*40}}
    with patch.object(collect, 'config_values', return_value=args), patch.object(cli, 'release_proof') as admit, \
            patch.object(proof, 'release_bindings', return_value={'capture': {'index': 'sha256:'+'9'*64}}):
        admit.side_effect = ValueError('initial copy admission corrupted')
        with pytest.raises(ValueError): cli.native_arguments(data, 'current/admitted.json')
        admit.side_effect = None; admit.return_value = 'old/admitted.json'
        derived = cli.native_arguments(data, 'current/admitted.json')
        assert derived.initial_copy_capture_digest == 'sha256:' + '9'*64
        assert derived.initial_copy_admission == 'old/admitted.json'
    assert 'initial_copy_capture_digest' not in collect.ALLOWED


def test_retained_manifest_must_precede_new_job_creation(tmp_path):
    manifest, probe, collector, jobs, _, _, _, current = validation_fixture(tmp_path)
    collector.probe = probe
    collector.oracle.return_value = {**current, 'reader': {}, 'loader': {}}
    probe.kubectl.return_value = {'items': jobs}
    # Fixture's current Job predates the manifest; an after-the-fact anchor is rejected.
    with patch.object(cli, 'revalidate_retained', return_value=[fixture('old')[-1]]):
        with pytest.raises(ValueError): cli.retained_oracle(collector, manifest)


@pytest.mark.parametrize('phase', ['snapshot', 'observe', 'evaluate', 'evaluate-crash'])
def test_history_phases_without_manifest_refuse_two_real_jobs_before_history_read(tmp_path, phase):
    from io import StringIO
    _, args, _, _ = fixture('new')
    _, old_args, _, _ = fixture('old')
    data = {'native': {}, 'release': {'mode': 'conserved', 'metadata_root': 'current', 'source_sha': 'a'*40},
            'refs': {'history_oracle': 'superficially-complete.json', 'baseline': 'old-baseline.json'}}
    config = tmp_path / 'config.json'; private(config, data)
    probe = Mock(args=args)
    probe.kubectl.return_value = {'items': [job(args, 'new.json'), job(old_args, 'old.json')]}
    with patch.object(cli, 'load_site', return_value=data), patch.object(cli, 'release_proof', return_value='admitted'), \
            patch.object(collect, 'config_values', return_value=args), patch.object(proof, 'NativeProbe', return_value=probe):
        output = StringIO()
        assert cli.run_native(str(config), str(tmp_path / 'out'), phase, stdout=output) == 1
        assert json.loads(output.getvalue())['native_cdc_qualified'] is False
    probe.snapshot.assert_not_called()
    probe.observe.assert_not_called()
    probe.snowflake.assert_not_called()
    probe.workload.assert_not_called()


def test_single_epoch_inventory_gate_does_not_recollect_oracle():
    _, args, _, _ = fixture('new')
    probe = Mock(args=args)
    probe.kubectl.return_value = {'items': [job(args, 'new.json')]}
    cli.history_inventory_gate(probe, None)
    probe.exec_json.assert_not_called()
    probe.source.assert_not_called()
    probe.snowflake.assert_not_called()


@pytest.mark.parametrize('phase', ['observe', 'evaluate', 'evaluate-crash'])
def test_history_verdict_revalidates_retained_bundle_before_using_old_baseline(tmp_path, phase):
    from io import StringIO
    manifest, probe, collector, jobs, config, _, _, _ = validation_fixture(tmp_path)
    manifest_ref = private(tmp_path / 'manifest.json', manifest)
    # The manifest itself is still correctly pinned, but its epoch changed since baseline.
    Path(manifest['epochs'][0]['bundle']['path']).write_text('{}')
    data = {'native': {'retained_history_manifest': manifest_ref['path'], 'retained_history_sha256': manifest_ref['sha256']},
            'release': {'mode': 'conserved', 'metadata_root': 'current', 'source_sha': 'a'*40},
            'refs': {'baseline': 'previous-pass.json'}}
    config_path = tmp_path / 'current.json'; private(config_path, data)
    probe.kubectl.return_value = {'items': jobs}
    probe.workload.return_value = ({'pod': 'current'}, {})
    with patch.object(cli, 'load_site', side_effect=lambda path, **_: data if str(path)==str(config_path) else config), \
            patch.object(cli, 'release_proof', return_value='admitted'), \
            patch.object(cli, 'native_arguments', return_value=probe.args), \
            patch.object(proof, 'NativeProbe', return_value=probe), patch.object(collect, 'LiveCollector', return_value=collector):
        output = StringIO()
        assert cli.run_native(str(config_path), str(tmp_path / 'out'), phase, stdout=output) == 1
        assert json.loads(output.getvalue())['native_cdc_qualified'] is False
    probe.observe.assert_not_called()
    probe.snowflake.assert_not_called()
    collector.copy.assert_not_called()  # SHA rejection precedes even immutable-object rereading.
