"""The installed CLI collects originals without admitting their native provenance."""
from dataclasses import asdict
from io import StringIO
import json

import pytest

from quadringent.installer.cli import run
from test_native_verification_collect import inputs, published  # noqa: F401
from test_native_verification_native import native_inputs  # noqa: F401


def plan(tmp_path, inputs):
    values, connections, _ = inputs
    def spec(process, name):
        path = tmp_path / (name + '.stdin')
        path.write_bytes(process.input_bytes)
        path.chmod(0o600)
        return dict(command=list(process.command), input_path=str(path), environment=dict(process.environment))
    scope = values['scope']
    config = dict(
        format_version='quadringent-collection-plan-v1', scope=scope.to_dict(),
        storage=dict(backend='local', root=str(values['store'].root)),
        raw_prefix_root=values['raw_prefix_root'],
        initial_copy=asdict(values['initial_copy']), copy_intent=asdict(values['copy_intent']),
        raw_read_set=[asdict(batch) for batch in values['raw_read_set'].batches],
        source=spec(values['source'], 'source'),
        journal=[spec(item.process, 'journal-' + str(i)) for i, item in enumerate(values['journal'])],
        positions=[spec(item.process, 'positions-' + str(i)) for i, item in enumerate(values['positions'])],
        destination=dict(account='fixture', user='FIXTURE', role='FIXTURE', warehouse='FIXTURE',
                         private_key_path=str(tmp_path / 'key.pem')),
        max_seconds=60,
    )
    (tmp_path / 'key.pem').write_text('SYNTHETIC_PRIVATE_KEY_CANARY')
    (tmp_path / 'key.pem').chmod(0o600)
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path, values, connections


def invoke(path, tmp_path):
    output = StringIO()
    status = run(['qualification', 'collect', '--config', str(path), '--out-dir', str(tmp_path / 'collected')],
                 stdout=output)
    return status, json.loads(output.getvalue())


def test_installed_command_runs_real_collection_without_native_claim(inputs, tmp_path, monkeypatch):
    from quadringent.qualification import verification_collect_cli as cli
    path, values, connections = plan(tmp_path, inputs)
    monkeypatch.setattr(cli, '_connect', lambda *_: values['connection_factory']())
    status, output = invoke(path, tmp_path)
    assert status == 0 and output['comparison_state'] == 'verified'
    assert output['native_cdc_qualified'] is False
    assert output['native_admission'] == 'not_evaluated'
    assert output['context_enforcement'] == 'local_files_only'
    assert len(connections) == 2 and all(connection.closed for connection in connections)
    assert 'SYNTHETIC_PRIVATE_KEY_CANARY' not in json.dumps(output)
    assert (tmp_path / 'collected' / 'comparison.json').is_file()
    inventory = json.loads((tmp_path / 'collected' / 'collection.json').read_text())
    assert inventory['native_cdc_qualified'] is False and inventory['native_admission'] == 'not_evaluated'
    assert output['collection_path'] == str(tmp_path / 'collected' / 'collection.json')
    assert invoke(path, tmp_path)[0] == 1


@pytest.mark.parametrize('mutation', ['permissions', 'symlink', 'duplicate', 'unknown', 'stdin_permissions'])
def test_invalid_private_plan_fails_before_process_or_connection(inputs, tmp_path, monkeypatch, mutation):
    from quadringent.qualification import verification_collect_cli as cli
    path, _, _ = plan(tmp_path, inputs)
    if mutation == 'permissions':
        path.chmod(0o644)
    elif mutation == 'symlink':
        actual = path.rename(tmp_path / 'actual.json')
        path.symlink_to(actual)
    elif mutation == 'duplicate':
        path.write_text(path.read_text().replace('{', '{"format_version":"forged",', 1))
    elif mutation == 'unknown':
        data = json.loads(path.read_text())
        data['native_admitted'] = True
        path.write_text(json.dumps(data))
    else:
        (tmp_path / 'source.stdin').chmod(0o644)
    def forbidden(*_):
        raise AssertionError('producer must not run')
    monkeypatch.setattr(cli, 'collect_verification', forbidden)
    monkeypatch.setattr(cli, '_connect', forbidden)
    status, output = invoke(path, tmp_path)
    assert status == 1 and output['native_cdc_qualified'] is False
    assert output['reason_code'] == 'collection_unavailable'
    assert not (tmp_path / 'collected').exists()


def test_local_plan_fence_detects_change_during_collection(inputs, tmp_path, monkeypatch):
    from quadringent.qualification import verification_collect_cli as cli
    path, _, _ = plan(tmp_path, inputs)
    def changing(*args, **kwargs):
        assert kwargs['check_current']() is True
        path.write_bytes(path.read_bytes() + b'\n')
        assert kwargs['check_current']() is False
        raise ValueError('SYNTHETIC_SECRET_ERROR_CANARY')
    monkeypatch.setattr(cli, 'collect_verification', changing)
    status, output = invoke(path, tmp_path)
    assert status == 1
    assert 'SYNTHETIC_SECRET_ERROR_CANARY' not in json.dumps(output)


def test_divergent_inputs_are_recorded_without_a_native_verdict(inputs, tmp_path, monkeypatch):
    from quadringent.qualification import verification_collect_cli as cli
    from test_native_verification_collect import process
    values, _, data = inputs
    rows = [dict(row) for row in data['source']]
    rows[0]['LABEL'] = 'different'
    changed = ({**values, 'source': process(values['scope'], 'source', rows)}, inputs[1], data)
    path, values, _ = plan(tmp_path, changed)
    monkeypatch.setattr(cli, '_connect', lambda *_: values['connection_factory']())
    status, output = invoke(path, tmp_path)
    assert status == 2 and output['comparison_state'] == 'divergent'
    assert output['native_cdc_qualified'] is False
    assert (tmp_path / 'collected' / 'collection.json').is_file()


def test_native_plan_requires_the_original_copy_observations_before_providers(inputs, tmp_path, monkeypatch):
    from quadringent.qualification import verification_collect_cli as cli
    path, _, _ = plan(tmp_path, inputs)
    document = json.loads(path.read_text())
    document['format_version'] = 'quadringent-collection-plan-v2'
    document['journal'] = dict(producer='ibmi-fixed-copy-v1', java='/fixture/java', classpath='/fixture/classes',
                               input_path=str(tmp_path / 'journal-0.stdin'), environment={})
    path.write_text(json.dumps(document))
    monkeypatch.setattr(cli, 'collect_verification', lambda *_args, **_kwargs: pytest.fail('old copy must refuse'))
    monkeypatch.setattr(cli, '_connect', lambda *_args: pytest.fail('must not open a destination connection'))
    status, output = invoke(path, tmp_path)
    assert status == 1 and output['native_cdc_qualified'] is False
    assert not (tmp_path / 'collected').exists()


def test_native_plan_materializes_and_fences_the_pinned_v3_descriptors(native_inputs, tmp_path):
    import hashlib
    from quadringent.qualification import verification_collect_cli as cli
    from quadringent.qualification.verification_stored import ObjectReference

    authority, scope, raw = native_inputs
    class RootStore:
        def get_bounded(self, key, maximum):
            if key == 'proof':
                return raw
            return authority.get_bounded(key.removeprefix('receiver-authority-v1/'), maximum)
    credentials = tmp_path / 'native.stdin'
    credentials.write_bytes(b'host\nuser\nprivate-native-secret\n')
    credentials.chmod(0o600)
    config = dict(producer='ibmi-fixed-copy-v1', java='/fixture/java', classpath='/fixture/classes',
                  input_path=str(credentials), environment={})
    local = cli._Inputs()
    processes, receipt = cli._native_journal(config, scope, RootStore(),
                                            ObjectReference('proof',hashlib.sha256(raw).hexdigest()),
                                            tmp_path/'collected', local, 60)
    assert processes[0].process.command[3] == 'io.quadringent.as400.NativeBusinessImageExport'
    assert processes[0].process.input_bytes == credentials.read_bytes()
    assert receipt['scope_sha256'] == scope.sha256
    assert receipt['native_admission'] == 'not_evaluated'
    bundle = cli._absolute(receipt['bundle_path'])
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == receipt['bundle_sha256']
    assert local.current() is True
    bundle.write_bytes(b'changed')
    assert local.current() is False


@pytest.mark.parametrize('failure', [None, 'incomplete', 'wrong_phase', 'file_differs', 'exit'])
def test_native_final_guard_runs_after_all_seven_streams(inputs, tmp_path, monkeypatch, failure):
    from pathlib import Path
    from quadringent.qualification import verification_collect_cli as cli
    from quadringent.qualification.verification_process import CollectedProcessDocument
    from quadringent.qualification.verification_collect import ProcessSpec

    path, values, connections = plan(tmp_path, inputs)
    document = json.loads(path.read_text())
    document['format_version'] = 'quadringent-collection-plan-v2'
    path.write_text(json.dumps(document))
    native_dir = tmp_path / 'native'
    native_dir.mkdir(mode=0o700)
    native = dict(
        scope_sha256=values['scope'].sha256, context_sha256='a'*64,
        capture_binding_sha256='b'*64, copy_boundary_sha256='c'*64,
        copy_run_id=values['scope'].copy_run_id, bundle_sha256='d'*64,
        guard_receipt_path=str(native_dir/'journal-guard.json'), native_admission='not_evaluated',
    )

    def guard(mode):
        return dict(
            format_version='quadringent-native-journal-guard-v1', mode=mode, state='complete',
            **{key: value for key, value in native.items() if key.endswith('sha256') or key == 'copy_run_id'},
            native_admission='not_evaluated', transaction_consistency_verified=False,
            error_code=None, guards=[dict(phase=phase) for phase in (
                ('before_business', 'after_business') if mode == 'journal' else ('after_all_streams',))],
        )

    def save(target, data):
        raw = (json.dumps(data) + '\n').encode()
        target.write_bytes(raw)
        target.chmod(0o600)
        return raw

    save(Path(native['guard_receipt_path']), guard('journal'))
    command = ('/fixture/java', '-cp', '/fixture/classes', 'io.quadringent.as400.NativeBusinessImageExport',
               *('fixture' for _ in range(27)))
    native['_producer'] = ProcessSpec(command, b'host\nuser\nNATIVE_SECRET_CANARY\n', {})

    monkeypatch.setattr(cli, '_native_journal', lambda *_: (values['journal'], native))
    monkeypatch.setattr(cli, '_connect', lambda *_: values['connection_factory']())
    calls = []

    def collect(command, *, directory, input_bytes, environment, validate, limits):
        calls.append(command)
        assert command[3] == 'io.quadringent.as400.NativeLifecycleGuard'
        assert len(command) == 31 and 1 <= int(command[-1]) <= 60
        assert input_bytes == b'host\nuser\nNATIVE_SECRET_CANARY\n'
        assert len(connections) == 2 and all(value.closed for value in connections)
        assert (tmp_path/'collected'/'comparison.json').is_file()
        assert all((tmp_path/'collected'/dataset).is_dir() for dataset in (
            'source', 'journal', 'positions', 'snapshot', 'raw', 'history', 'mirror'))
        if failure == 'exit':
            raise ValueError('guard_process_exit')
        data = guard('after_all_streams')
        if failure == 'incomplete':
            data['state'] = 'incomplete'
        elif failure == 'wrong_phase':
            data['guards'][0]['phase'] = 'before_business'
        validate(data)
        directory.mkdir(mode=0o700)
        original = directory/'original.json'
        raw = save(original, data)
        save(Path(command[27]), {**data, 'state':'incomplete'} if failure == 'file_differs' else data)
        import hashlib
        return CollectedProcessDocument(original, data, hashlib.sha256(raw).hexdigest(), len(raw))

    monkeypatch.setattr(cli, 'collect_process_document', collect, raising=False)
    status, output = invoke(path, tmp_path)
    assert len(calls) == 1
    assert status == (0 if failure is None else 1)
    assert output['native_cdc_qualified'] is False
    assert 'NATIVE_SECRET_CANARY' not in json.dumps(output)
    if failure is None:
        receipt = json.loads(Path(output['native_copy_inputs_path']).read_text())
        assert receipt['final_guard']['sha256']
        assert receipt['final_guard']['mode'] == 'after_all_streams'
        assert receipt['native_admission'] == 'not_evaluated'
        assert '_producer' not in receipt
