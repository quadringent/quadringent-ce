from argparse import Namespace
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from types import ModuleType, SimpleNamespace

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent_control_plane.v2.executor.boundary import JournalBoundary


def fixture():
    identity = ReceiverIdentity('S', 'J', 'S', 'R1', 'synthetic-v1', 'generation-a')
    bootstrap = BoundCheckpoint('scope', 'epoch', identity, 5)
    binding = CaptureBinding('scope', 'epoch', 'ibmi', 'J', 's3://synthetic-bucket/receiver-authority-v1', bootstrap)
    point = binding.checkpoint(identity, 10)
    boundary = JournalBoundary('S', 'R1', 10, datetime(2026, 1, 1, tzinfo=UTC), identity, binding, point)
    evidence = {'format_version': 'quadringent-initial-copy-evidence-v2', 'pipeline_id': 'p1', 'table_id': 't1',
                'run_id': 'run1', 'boundary': boundary.to_dict(), 'rows_copied': 1,
                'completed_at': '2026-01-01T00:00:00+00:00', 'snapshot_batches': []}
    args = Namespace(pipeline='p1', table_id='t1', copy_run_id='run1', copy_boundary='R1:10',
                     journal_library='S', journal_name='J', expected_revision='a' * 40)
    return evidence, args, binding


def test_v2_boundary_retains_exact_original_binding_and_position():
    from quadringent.qualification.boundary import read_copy_boundary

    evidence, args, binding = fixture()
    original = deepcopy(evidence)
    parsed = read_copy_boundary(evidence, args, expected_binding=binding)
    assert parsed.require_bound().sequence == 10
    assert parsed.capture_binding == binding
    assert parsed.to_dict() == original['boundary']
    assert evidence == original


@pytest.mark.parametrize('fault', ['pipeline', 'table', 'run', 'sequence', 'epoch', 'scope', 'generation', 'journal', 'authority', 'label'])
def test_v2_tampered_scope_binding_or_original_identity_refused(fault):
    from quadringent.qualification.boundary import read_copy_boundary

    evidence, args, binding = fixture()
    if fault in {'pipeline', 'table', 'run'}:
        evidence[{'pipeline': 'pipeline_id', 'table': 'table_id', 'run': 'run_id'}[fault]] = 'foreign'
    elif fault == 'label':
        args.copy_boundary = 'R1:11'
    elif fault == 'journal':
        args.journal_name = 'FOREIGN'
    elif fault == 'generation':
        evidence['boundary']['receiver_identity']['native_generation'] = 'generation-b'
    elif fault == 'authority':
        evidence['boundary']['capture_binding']['registry_authority'] = 's3://foreign-bucket/receiver-authority-v1'
    else:
        field = {'epoch': 'capture_epoch', 'scope': 'scope_id', 'sequence': 'sequence'}[fault]
        evidence['boundary']['bound_checkpoint'][field] = 11 if field == 'sequence' else 'foreign'
    with pytest.raises(ValueError):
        read_copy_boundary(evidence, args, expected_binding=binding)


def test_current_runtime_refuses_unbound_legacy_copy():
    from quadringent.qualification.boundary import read_copy_boundary

    evidence, args, _ = fixture()
    evidence['boundary'] = {'receiver_name': 'R1', 'receiver_library': 'S', 'last_sequence': 10}
    with pytest.raises(ValueError, match='bound'):
        read_copy_boundary(evidence, args)


def test_bound_checkpoint_identity_and_scope_are_required():
    from quadringent.qualification.boundary import read_copy_boundary, read_checkpoint

    evidence, args, binding = fixture()
    boundary = read_copy_boundary(evidence, args, expected_binding=binding)
    point = binding.checkpoint(binding.bootstrap.receiver_identity, 12)
    record = {'receiver': 'R1', 'sequence': 12, 'bound_checkpoint': point.to_dict(), 'capture_binding': binding.to_dict()}
    assert read_checkpoint(record, boundary) == point
    for field in ('scope_id', 'capture_epoch'):
        bad = deepcopy(record)
        bad['bound_checkpoint'][field] = 'foreign'
        with pytest.raises(ValueError):
            read_checkpoint(bad, boundary)
    bad = deepcopy(record)
    bad['bound_checkpoint']['receiver_identity']['native_generation'] = 'reused-name'
    with pytest.raises(ValueError):
        read_checkpoint(bad, boundary)
    with pytest.raises(ValueError):
        read_checkpoint({'receiver': 'R1', 'sequence': 12}, boundary)


def test_same_receiver_native_v2_oracle_is_readable_without_flattening():
    from quadringent.qualification.collect import validate_oracle_parts
    from test_native_qualification_collect import OracleCollectorTests

    copy, journal, positions, checkpoint, args, columns = OracleCollectorTests().fixture()
    evidence, bound_args, binding = fixture()
    copy['boundary'] = evidence['boundary']
    for name in ('format_version', 'completed_at', 'snapshot_batches'):
        copy[name] = evidence[name]
    args.expected_revision = bound_args.expected_revision
    checkpoint.update(bound_checkpoint=binding.checkpoint(binding.bootstrap.receiver_identity, 11).to_dict(),
                      capture_binding=binding.to_dict())
    result = validate_oracle_parts(copy, journal, positions, checkpoint, args, columns)
    assert len(result['events']) == 2
    assert copy['boundary']['format_version'] == 'quadringent-journal-boundary-v2'


@pytest.mark.parametrize('fault', ['stripped-version', 'stripped-boundary', 'source-system'])
def test_native_copy_cannot_be_downgraded_to_legacy_inspection(fault):
    from quadringent.qualification.boundary import read_copy_boundary

    evidence, args, _ = fixture()
    args.expected_revision = None
    if fault == 'stripped-version':
        del evidence['boundary']['format_version']
        evidence['boundary'].update(receiver_name='R1', receiver_library='S', last_sequence=10)
    elif fault == 'stripped-boundary':
        evidence['boundary'] = {'receiver_name': 'R1', 'receiver_library': 'S', 'last_sequence': 10}
    else:
        evidence['boundary']['capture_binding']['source_system'] = 'foreign'
    with pytest.raises(ValueError):
        read_copy_boundary(evidence, args)


def test_admitted_historical_legacy_copy_is_preserved():
    from quadringent.qualification.boundary import read_copy_boundary

    evidence, args, _ = fixture()
    del evidence['format_version']
    evidence['boundary'] = {'receiver_name': 'R1', 'receiver_library': 'S', 'last_sequence': 10}
    args.initial_copy_admission = 'historical-admission'
    args.initial_copy_revision = 'historical-revision'
    original = deepcopy(evidence)
    assert read_copy_boundary(evidence, args, historical=True).last_sequence == 10
    assert evidence == original


def test_historical_admission_never_relaxes_current_copy_or_checkpoint():
    from quadringent.qualification.boundary import read_copy_boundary, legacy_inspection_allowed

    evidence, args, _ = fixture()
    del evidence['format_version']
    evidence['boundary'] = {'receiver_name': 'R1', 'receiver_library': 'S', 'last_sequence': 10}
    args.initial_copy_admission = 'historical-admission'
    args.initial_copy_revision = 'historical-revision'
    assert legacy_inspection_allowed(args) is False
    with pytest.raises(ValueError, match='bound'):
        read_copy_boundary(evidence, args)


@pytest.mark.parametrize('fault', ['sequence-bool', 'sequence-mismatch', 'regression', 'authority', 'receiver'])
def test_checkpoint_cannot_change_or_regress_the_original_native_position(fault):
    from quadringent.qualification.boundary import read_copy_boundary, read_checkpoint

    evidence, args, binding = fixture()
    boundary = read_copy_boundary(evidence, args)
    record = {'receiver': 'R1', 'sequence': 12,
              'bound_checkpoint': binding.checkpoint(binding.bootstrap.receiver_identity, 12).to_dict(),
              'capture_binding': binding.to_dict()}
    if fault == 'sequence-bool':
        record['sequence'] = True
        record['bound_checkpoint']['sequence'] = 1
    elif fault == 'sequence-mismatch':
        record['sequence'] = 13
    elif fault == 'regression':
        record['sequence'] = record['bound_checkpoint']['sequence'] = 9
    elif fault == 'authority':
        record['capture_binding']['registry_authority'] = 's3://foreign-bucket/receiver-authority-v1'
    else:
        record['receiver'] = 'R2'
    with pytest.raises(ValueError):
        read_checkpoint(record, boundary)


def gcp_fixture(monkeypatch, evidence, binding):
    from quadringent.qualification.proof import NativeProbe

    _, args, _ = fixture()
    args.expected_gcp_project = 'qualification-project'
    args.expected_gcp_service_account = 'reader@qualification-project.iam.gserviceaccount.com'
    args.expected_gcs_bucket = 'synthetic-bucket'
    args.gcs_read_key = 'copy/proof.json'
    args.table = 'S.T'
    probe = object.__new__(NativeProbe)
    probe.args = args
    payload = (' \n' + json.dumps(evidence, indent=2) + '\n').encode()
    blob = SimpleNamespace(size=len(payload), generation=7, reload=lambda **kw: None,
                           download_as_bytes=lambda **kw: payload)

    class Credentials:
        service_account_email = args.expected_gcp_service_account

        def refresh(self, request):
            pass

    modules = {name: ModuleType(name) for name in (
        'google', 'google.auth', 'google.auth.compute_engine', 'google.auth.compute_engine.credentials',
        'google.auth.compute_engine._metadata', 'google.auth.transport', 'google.auth.transport.requests',
        'google.cloud', 'google.cloud.storage')}
    modules['google'].auth = modules['google.auth']
    modules['google.auth'].default = lambda **kw: (Credentials(), args.expected_gcp_project)
    modules['google.auth.compute_engine.credentials'].Credentials = Credentials
    modules['google.auth.compute_engine._metadata'].get_project_id = lambda request: args.expected_gcp_project
    modules['google.auth.transport.requests'].Request = type('Request', (), {})
    modules['google.cloud.storage'].Client = lambda **kw: SimpleNamespace(
        bucket=lambda name: SimpleNamespace(blob=lambda key: blob))
    for name, module in modules.items():
        monkeypatch.setitem(__import__('sys').modules, name, module)
    for name in ('GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_API_KEY', 'QUADRINGENT_LOADER_TABLE_SET_JSON'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('QUADRINGENT_STORAGE_BACKEND', 'gcs')
    monkeypatch.setenv('AS400_RAW_BUCKET', args.expected_gcs_bucket)
    monkeypatch.setenv('AS400_CAPTURE_BINDING_JSON', json.dumps(binding.to_dict()))
    monkeypatch.setenv('AS400_STREAM_KEY', binding.scope_id)
    return probe, payload


def gcs_copy():
    evidence, _, binding = fixture()
    binding = CaptureBinding(binding.scope_id, binding.capture_epoch, binding.source_system, binding.journal,
                             'gcs://synthetic-bucket/receiver-authority-v1', binding.bootstrap)
    boundary = JournalBoundary.from_dict(evidence['boundary'])
    evidence['boundary'] = JournalBoundary(boundary.receiver_library, boundary.receiver_name, boundary.last_sequence,
                                          boundary.observed_at, boundary.receiver_identity, binding,
                                          binding.checkpoint(boundary.receiver_identity, boundary.last_sequence)).to_dict()
    return evidence, binding


def test_gcp_identity_script_reads_v2_original_with_exact_raw_hash(monkeypatch, capsys):
    evidence, binding = gcs_copy()
    original = deepcopy(evidence)
    probe, payload = gcp_fixture(monkeypatch, evidence, binding)
    script = probe.gke_identity_script()
    exec(compile(script, 'gcp-identity-test', 'exec'), {})
    result = json.loads(capsys.readouterr().out)
    assert result['gcs_read']['sha256'] == hashlib.sha256(payload).hexdigest()
    assert result['gcs_read']['bytes'] == len(payload)
    assert evidence == original


@pytest.mark.parametrize('fault', ['table', 'run', 'journal', 'authority', 'scope', 'generation', 'sequence', 'legacy'])
def test_gcp_identity_script_rejects_tampered_original_scope(monkeypatch, fault):
    evidence, binding = gcs_copy()
    if fault in {'table', 'run'}:
        evidence[{'table': 'table_id', 'run': 'run_id'}[fault]] = 'foreign'
    elif fault == 'journal':
        evidence['boundary']['receiver_identity']['journal_name'] = 'OTHER'
    elif fault == 'authority':
        evidence['boundary']['capture_binding']['registry_authority'] = 'gcs://foreign-bucket/receiver-authority-v1'
    elif fault == 'scope':
        evidence['boundary']['bound_checkpoint']['scope_id'] = 'foreign'
    elif fault == 'generation':
        evidence['boundary']['receiver_identity']['native_generation'] = 'reused-name'
    elif fault == 'sequence':
        evidence['boundary']['bound_checkpoint']['sequence'] = 11
    else:
        del evidence['format_version']
        evidence['boundary'] = {'receiver_name': 'R1', 'receiver_library': 'S', 'last_sequence': 10}
    probe, _ = gcp_fixture(monkeypatch, evidence, binding)
    with pytest.raises((ValueError, SystemExit)):
        exec(compile(probe.gke_identity_script(), 'gcp-identity-test', 'exec'), {})


def test_runtime_checkpoint_keeps_native_binding_and_uses_bound_store(monkeypatch, capsys):
    from quadringent.qualification.proof import NativeProbe
    from quadringent.storage_backend import StorageBackend
    from quadringent.receiver_registry import ReceiverNameRegistry

    _, args, binding = fixture()
    point = binding.checkpoint(binding.bootstrap.receiver_identity, 12)
    seen = []
    store = SimpleNamespace(load_bound=lambda: point)
    backend = SimpleNamespace(kind='aws', raw_bucket='synthetic-bucket', checkpoint_store=lambda key: store,
                              object_store=lambda prefix: object())
    monkeypatch.setattr(StorageBackend, 'from_environment', classmethod(lambda cls, env: backend))
    monkeypatch.setattr(ReceiverNameRegistry, 'require', lambda self, b, p: seen.append((b, p)))
    probe = object.__new__(NativeProbe)
    probe.args = args

    def execute(pod, container, script):
        exec(compile(script, 'checkpoint-test', 'exec'), {})
        return json.loads(capsys.readouterr().out)

    probe.exec_json = execute
    env = {'QUADRINGENT_STORAGE_BACKEND': 'aws', 'AS400_STREAM_KEY': binding.scope_id,
           'AS400_RAW_BUCKET': 'synthetic-bucket', 'AS400_CHECKPOINT_TABLE': 'state',
           'AS400_CAPTURE_BINDING_JSON': json.dumps(binding.to_dict())}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    original = deepcopy(env)
    result = probe.checkpoint(({}, env), ({'pod': 'loader', 'container': 'destination-loader'}, {}))
    assert result == {'receiver': 'R1', 'sequence': 12, 'bound_checkpoint': point.to_dict(),
                      'capture_binding': binding.to_dict()}
    assert seen == [(binding, point)]
    assert env == original


def test_installed_checkpoint_refuses_missing_runtime_binding():
    from quadringent.qualification.proof import NativeProbe
    from unittest.mock import Mock

    _, args, _ = fixture()
    args.initial_copy_admission = 'historical-admission'
    args.initial_copy_revision = 'historical-revision'
    probe = object.__new__(NativeProbe)
    probe.args, probe.exec_json = args, Mock()
    env = {'QUADRINGENT_STORAGE_BACKEND': 'aws', 'AS400_STREAM_KEY': 'scope',
           'AS400_RAW_BUCKET': 'synthetic-bucket', 'AS400_CHECKPOINT_TABLE': 'state'}
    with pytest.raises(ValueError, match='native runtime binding'):
        probe.checkpoint(({}, env), ({'pod': 'loader', 'container': 'destination-loader'}, {}))
    probe.exec_json.assert_not_called()


@pytest.mark.parametrize('fault', ['missing', 'legacy', 'epoch', 'scope', 'generation', 'authority', 'flat-sequence', 'regression', 'bootstrap-regression'])
def test_installed_recovery_never_accepts_changed_native_checkpoint(tmp_path, fault):
    from quadringent.qualification import cli
    from test_native_qualification_cli import fixture as recovery_fixture

    data = recovery_fixture(tmp_path)
    before, after = data[:2]
    _, _, binding = fixture()
    for snapshot in (before, after):
        point = binding.checkpoint(binding.bootstrap.receiver_identity, snapshot['checkpoint']['sequence'])
        snapshot['checkpoint'].update(bound_checkpoint=point.to_dict(), capture_binding=binding.to_dict())
    checkpoint = after['checkpoint']
    if fault == 'missing':
        del checkpoint['bound_checkpoint']
    elif fault == 'legacy':
        for snapshot in (before, after):
            del snapshot['checkpoint']['bound_checkpoint'], snapshot['checkpoint']['capture_binding']
    elif fault == 'flat-sequence':
        checkpoint['sequence'] = 12
    elif fault == 'regression':
        checkpoint['sequence'] = checkpoint['bound_checkpoint']['sequence'] = 9
    elif fault == 'bootstrap-regression':
        for snapshot in (before, after):
            snapshot['checkpoint']['capture_binding']['bootstrap']['sequence'] = 12
    elif fault == 'generation':
        checkpoint['bound_checkpoint']['receiver_identity']['native_generation'] = 'reused-name'
    elif fault == 'authority':
        checkpoint['capture_binding']['registry_authority'] = 's3://foreign-bucket/receiver-authority-v1'
    else:
        field = {'epoch': 'capture_epoch', 'scope': 'scope_id'}[fault]
        checkpoint['bound_checkpoint'][field] = 'foreign'
        checkpoint['capture_binding'][field] = 'foreign'
        checkpoint['capture_binding']['bootstrap'][field] = 'foreign'
    data[5].write_text(json.dumps(before))
    data[3][0]['baseline_sha256'] = cli.sha(data[5])
    with pytest.raises(ValueError):
        cli.evaluate(*data, expected_history_mode='streaming')


def test_installed_resume_preserves_original_native_checkpoint_records(tmp_path):
    from quadringent.qualification import cli
    from test_native_qualification_cli import fixture as recovery_fixture

    data = recovery_fixture(tmp_path)
    original = deepcopy(data[:3])
    assert cli.evaluate(*data, expected_history_mode='streaming')['native_cdc_qualified'] is True
    assert data[:3] == original


@pytest.mark.parametrize('fault', ['legacy', 'epoch'])
def test_installed_crash_refuses_legacy_or_changed_capture_epoch(fault):
    from quadringent.qualification.proof import evaluate_crash
    from test_native_qualification_crash import crash_fixture

    before, after, crash, mutations = crash_fixture()
    checkpoint = after['checkpoint']
    if fault == 'legacy':
        del checkpoint['bound_checkpoint'], checkpoint['capture_binding']
    else:
        for record in (checkpoint['bound_checkpoint'], checkpoint['capture_binding'], checkpoint['capture_binding']['bootstrap']):
            record['capture_epoch'] = 'foreign'
    with pytest.raises(ValueError):
        evaluate_crash(before, after, {'crash': crash, 'mutations': mutations})


@pytest.mark.parametrize(('bound', 'historical'), [(False, False), (True, False), (True, True)])
def test_installed_rotation_cannot_bypass_native_generation_proof(bound, historical):
    from quadringent.qualification.proof import evaluate_rotation
    from test_native_qualification_rotation import fixture as rotation_fixture

    before, after, operations = rotation_fixture()
    if bound:
        _, _, binding = fixture()
        for snapshot in (before, after):
            name = snapshot['checkpoint']['receiver']
            identity = binding.bootstrap.receiver_identity if name == 'R1' else ReceiverIdentity(
                'S', 'J', 'S', name, 'synthetic-v1', 'generation-b')
            point = binding.checkpoint(identity, snapshot['checkpoint']['sequence'])
            snapshot['checkpoint'].update(bound_checkpoint=point.to_dict(), capture_binding=binding.to_dict())
    with pytest.raises(ValueError):
        evaluate_rotation(before, after, operations, historical=historical)


def test_installed_rotation_binding_refuses_legacy_despite_old_admission():
    from quadringent.qualification.collect import bind_rotation_mutation_receipts
    from test_native_qualification_rotation_binding import inputs

    data = inputs()
    data[-1].expected_revision = 'a' * 40
    data[-1].initial_copy_revision = 'historical-revision'
    data[-1].initial_copy_admission = 'historical-admission'
    with pytest.raises(ValueError):
        bind_rotation_mutation_receipts(*data)


def copy_script_fixture(tmp_path, monkeypatch, capsys):
    from quadringent.contract import ChangeEvent, JournalPosition
    from quadringent.qualification.collect import LiveCollector
    from quadringent.raw import RawBatchWriter, bind_snapshot_batch, read_raw_batch
    from quadringent.receiver_registry import ReceiverNameRegistry
    from quadringent.storage_backend import StorageBackend

    evidence, args, binding = fixture()
    args.table = 'S.T'
    args.database, args.schema, args.pk = 'DB', 'DEV', 'ID'
    args.history, args.mirror = 'T_HISTORY', 'T_MIRROR'
    event = ChangeEvent('ibmi', 'SNAPSHOT:S.T', 'S', 'T', 'c', JournalPosition('SNAPSHOT:run1', 1),
                        '2026-01-01T00:00:00Z', 'schema', None, {'ID': 1, 'NOTE': 'original'})
    original_manifest = RawBatchWriter(tmp_path).write_batch([event], high_watermark=event.position)
    payload = (tmp_path / ('batch-' + original_manifest.batch_id + '.jsonl')).read_bytes()
    legacy_manifest = (tmp_path / ('batch-' + original_manifest.batch_id + '.manifest.json')).read_bytes()
    manifest = bind_snapshot_batch(legacy_manifest, payload, binding=binding,
                                   boundary=JournalBoundary.from_dict(evidence['boundary']).require_bound(),
                                   pipeline_id='p1', table_id='t1', run_id='run1')
    batch_id = read_raw_batch(manifest, payload).manifest.batch_id
    ref = {'payload_key': 'batch-' + batch_id + '.jsonl', 'manifest_key': 'batch-' + batch_id + '.manifest.json'}
    evidence['snapshot_batches'] = [ref]
    raw = ('\n' + json.dumps(evidence, indent=2) + '\n').encode()
    key = 'raw/t1/evidence/run1.json'

    class Store:
        def __init__(self, values=None):
            self.values = values or {}

        def get_bounded(self, name, maximum):
            if name not in self.values:
                raise FileNotFoundError(name)
            value = self.values[name]
            assert len(value) <= maximum
            return value

        def put_once(self, name, value):
            assert name not in self.values
            self.values[name] = value

    stores = {'': Store({key: raw}), 't/snapshot': Store({ref['payload_key']: payload, ref['manifest_key']: manifest}),
              'receiver-authority-v1': Store()}
    backend = SimpleNamespace(kind='aws', raw_bucket='synthetic-bucket', object_store=lambda prefix: stores[prefix])
    registry = ReceiverNameRegistry(stores['receiver-authority-v1'], authority=binding.registry_authority,
                                    source_system='ibmi', journal='J')
    registry.store.put_once(registry.binding_key(binding.scope_id), json.dumps(binding.to_dict()).encode())
    registry._claim_point(binding, binding.bootstrap)
    monkeypatch.setattr(StorageBackend, 'from_environment', classmethod(lambda cls, env: backend))
    monkeypatch.setenv('QUADRINGENT_LOADER_TABLE_SET_JSON', '[]')
    monkeypatch.setenv('QUADRINGENT_DESTINATION_DATABASE', args.database)
    monkeypatch.setenv('QUADRINGENT_DESTINATION_SCHEMA', args.schema)
    monkeypatch.delenv('AS400_RAW_PREFIX', raising=False)
    table = SimpleNamespace(table_id='t1', pipeline_id='p1', run_id='run1', capture_binding=binding,
                            evidence_key=key, schema_name='S', table_name='T', key_columns=('ID',),
                            columns=[{'name': 'ID', 'kind': 'int'}, {'name': 'NOTE', 'kind': 'varchar'}])
    loader = SimpleNamespace(parse_table_set=lambda raw: [table], build_plan=lambda *a, **kw: SimpleNamespace(
        history_table=args.history, mirror_table=args.mirror))

    def execute(pod, container, script):
        exec(compile(script, 'initial-copy-test', 'exec'), {'l': loader})
        return json.loads(capsys.readouterr().out)

    probe = SimpleNamespace(args=args, columns={'ID': 'int', 'NOTE': 'text'}, library='S', table='T',
                            loader_import_script=lambda alias: '', exec_json=execute)
    return LiveCollector(probe), table, stores, evidence, raw, ref, legacy_manifest


def test_live_copy_script_preserves_original_native_json_and_raw_bytes(tmp_path, monkeypatch, capsys):
    collector, _, stores, evidence, raw, ref, _ = copy_script_fixture(tmp_path, monkeypatch, capsys)
    originals = {prefix: deepcopy(store.values) for prefix, store in stores.items()}
    result = collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
    assert {key: result[key] for key in evidence} == evidence
    assert result['artifacts'][0]['sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['artifacts'][1]['payload_sha256'] == hashlib.sha256(stores['t/snapshot'].values[ref['payload_key']]).hexdigest()
    assert {prefix: store.values for prefix, store in stores.items()} == originals


@pytest.mark.parametrize('fault', ['table-run', 'table-binding', 'snapshot-legacy', 'registry-binding', 'registry-generation'])
def test_live_copy_script_refuses_runtime_or_snapshot_binding_tampering(tmp_path, monkeypatch, capsys, fault):
    collector, table, stores, _, _, ref, legacy_manifest = copy_script_fixture(tmp_path, monkeypatch, capsys)
    if fault == 'table-run':
        table.run_id = 'foreign'
    elif fault == 'table-binding':
        table.capture_binding = None
    elif fault == 'snapshot-legacy':
        stores['t/snapshot'].values[ref['manifest_key']] = legacy_manifest
    else:
        for key, raw in stores['receiver-authority-v1'].values.items():
            record = json.loads(raw)
            if fault == 'registry-binding' and record['format_version'] == CaptureBinding.FORMAT_VERSION:
                record['capture_epoch'] = 'foreign'
            elif fault == 'registry-generation' and record.get('state') == 'observed':
                record['first_checkpoint']['receiver_identity']['native_generation'] = 'reused-name'
            stores['receiver-authority-v1'].values[key] = json.dumps(record).encode()
    with pytest.raises((ValueError, SystemExit)):
        collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))


@pytest.mark.parametrize('tamper', [False, True, 'foreign-source', 'foreign-prefix', 'foreign-scope', 'race-manifest'])
def test_live_copy_v3_script_archives_and_revalidates_observation_originals(tmp_path, monkeypatch, capsys, tamper):
    from quadringent.copy_observation import build_context, publish_copy_observations
    from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence
    from test_copy_observation import request, observation, encode

    collector, table, stores, evidence, _, ref, _ = copy_script_fixture(tmp_path, monkeypatch, capsys)
    current_boundary = JournalBoundary.from_dict(evidence['boundary'])
    req = request()
    req['source'].update(library='S', file='OTHER' if tamper == 'foreign-source' else 'T', member='T', format='TFMT')
    point = current_boundary.require_bound()
    req['witness_window'].update(receiver_library=point.receiver_identity.receiver_library,
                                receiver_name=point.receiver_identity.receiver_name, start='1', end=str(point.sequence))
    pipeline_id = 'foreign' if tamper == 'foreign-scope' else 'p1'
    current = build_context(pipeline_id=pipeline_id, table_id='t1', run_id='run1', boundary=current_boundary, request=req)
    item = {'prefix': 'unowned/snapshot' if tamper == 'foreign-prefix' else 't/snapshot', **ref}
    for part in ('payload', 'manifest'):
        data = stores['t/snapshot'].values[ref[part + '_key']]
        item[part + '_sha256'] = hashlib.sha256(data).hexdigest()
        item[part + '_bytes'] = len(data)
    reference = publish_copy_observations(stores['receiver-authority-v1'], context=current,
                                          before_bytes=encode(observation(current=current)),
                                          after_bytes=encode(observation('after', current)),
                                          snapshot_inventory=[item], rows_copied=1,
                                          evidence_key=table.evidence_key)
    proof = InitialCopyEvidence.from_dict(evidence)
    from dataclasses import replace
    raw = encode(replace(proof, pipeline_id=pipeline_id, copy_observations=reference).to_dict())
    stores[''].values[table.evidence_key] = raw
    for key, data in stores['receiver-authority-v1'].values.items():
        stores[''].values['receiver-authority-v1/' + key] = data
    if tamper in {'foreign-source', 'foreign-prefix', 'foreign-scope'}:
        reads = []
        get = stores['t/snapshot'].get_bounded
        def record_read(name, maximum):
            reads.append(name)
            return get(name, maximum)
        monkeypatch.setattr(stores['t/snapshot'], 'get_bounded', record_read)
        with pytest.raises(ValueError):
            collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
        assert reads == []
        return
    if tamper == 'race-manifest':
        get = stores['t/snapshot'].get_bounded
        manifest_reads = []
        def racing_read(name, maximum):
            data = get(name, maximum)
            if name == ref['manifest_key']:
                manifest_reads.append(name)
                if len(manifest_reads) == 2:
                    return data + b' '
            return data
        monkeypatch.setattr(stores['t/snapshot'], 'get_bounded', racing_read)
        with pytest.raises(ValueError):
            collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
        return
    if tamper is True:
        stores['t/snapshot'].values[ref['manifest_key']] += b' '
        with pytest.raises(ValueError):
            collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
        return
    result = collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
    expected = {'receiver-authority-v1/' + key for key in stores['receiver-authority-v1'].values
                if key.startswith('copy-observations-v1/')}
    actual = {item.get('evidence_key') for item in result['artifacts']}
    assert expected <= actual
    del stores[''].values['receiver-authority-v1/' + reference.key]
    with pytest.raises((ValueError, FileNotFoundError)):
        collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
