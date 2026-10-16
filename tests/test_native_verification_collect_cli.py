"""The installed CLI collects originals without admitting their native provenance."""
from dataclasses import asdict
from io import StringIO
import json

import pytest

from quadringent.installer.cli import run
from test_native_verification_collect import inputs, published  # noqa: F401


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
