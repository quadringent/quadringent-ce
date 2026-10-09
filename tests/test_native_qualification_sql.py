"""Le mode SQL conserve les mêmes preuves natives, sans reçu SDK inventé."""
from copy import deepcopy

import pytest

import test_native_qualification_cli as fixtures
from quadringent.qualification import cli, proof


def sql_fixture(tmp_path):
    data = list(fixtures.fixture(tmp_path))
    for snapshot in data[:2]:
        snapshot['history_mode'] = 'sql'
    data[3][0]['history_mode'] = 'sql'
    fixtures.private(data[5], data[0])
    data[3][0]['baseline_sha256'] = cli.sha(data[5])
    return data


def test_sql_resume_and_visibility_accept_same_proofs_without_sdk_offsets(tmp_path):
    data = sql_fixture(tmp_path)
    result = cli.evaluate(*data, expected_history_mode='sql')
    assert result['native_cdc_qualified'] is True
    assert result['maximum_10s_qualified'] is False
    assert result['history_mode'] == 'sql'
    assert all('offset_token' not in snapshot for snapshot in data[:2])


@pytest.mark.parametrize('layer', ['before', 'after', 'observation'])
def test_sql_refuses_any_observed_streaming_mode(tmp_path, layer):
    data = sql_fixture(tmp_path)
    target = {'before': data[0], 'after': data[1], 'observation': data[3][0]}[layer]
    target['history_mode'] = 'streaming'
    with pytest.raises(ValueError):
        cli.evaluate(*data, expected_history_mode='sql')


@pytest.mark.parametrize('expected', ['sql', 'streaming'])
def test_shared_mode_gate_keeps_crash_and_rotation_mode_exact(expected):
    snapshots = [{'history_mode': expected}, {'history_mode': expected}]
    proof.validate_history_modes(expected, *snapshots)
    other = deepcopy(snapshots)
    other[1]['history_mode'] = 'streaming' if expected == 'sql' else 'sql'
    with pytest.raises(ValueError):
        proof.validate_history_modes(expected, *other)


@pytest.mark.parametrize('mode', [None, '', 'SQL', 'copy_merge', ' sql '])
def test_shared_mode_gate_refuses_unknown_or_missing_modes(mode):
    with pytest.raises(ValueError):
        proof.validate_history_modes(mode, {'history_mode': mode})


@pytest.mark.parametrize('mode', ['sql', 'streaming'])
def test_native_config_accepts_only_supported_explicit_modes(mode):
    import test_native_qualification_collect as collect_fixtures
    from quadringent.qualification import collect
    data = collect_fixtures.GcpVmConfigTests().config()
    data['expected_history_mode'] = mode
    assert collect.config_values(data).expected_history_mode == mode
    data['expected_history_mode'] = 'copy_merge'
    with pytest.raises(ValueError):
        collect.config_values(data)


def test_retained_streaming_epoch_keeps_its_own_mode_in_sql_migration():
    import test_native_qualification_retained as retained_fixtures
    snapshot, oracle, old_probe = retained_fixtures.snapshot_fixture()
    old_probe.args.expected_history_mode = 'streaming'
    cli.validate_retained_snapshot(snapshot, oracle, old_probe)
    snapshot['history_mode'] = 'sql'
    with pytest.raises(ValueError):
        cli.validate_retained_snapshot(snapshot, oracle, old_probe)


@pytest.mark.parametrize('mode', ['sql', 'streaming'])
def test_same_reader_crash_contract_preserves_all_history_in_both_modes(mode):
    import test_native_qualification_crash as crash_fixtures
    before, after, crash, mutations = crash_fixtures.crash_fixture()
    before['history_mode'] = after['history_mode'] = mode
    proof.validate_history_modes(mode, before, after)
    result = proof.evaluate_crash(before, after, {'crash': crash, 'mutations': mutations})
    assert result['crash_recovery_qualified'] is True
    assert result['loader_crash_qualified'] is False
    after['history'] = []
    with pytest.raises(ValueError):
        proof.evaluate_crash(before, after, {'crash': crash, 'mutations': mutations})


@pytest.mark.parametrize('actual_mode', ['sql', 'streaming'])
def test_actual_sql_probe_requires_matching_loader_mode_without_sdk(actual_mode):
    from argparse import Namespace
    from unittest.mock import Mock
    probe = object.__new__(proof.NativeProbe)
    probe.columns = {'ID': 'int', 'NOTE': 'text'}
    probe.library = 'SOURCE'
    probe.table = 'ORDERS'
    probe.args = Namespace(database='DB', schema='SCHEMA', table_id='table1', pk='ID',
                           history='ORDERS_HISTORY', mirror='ORDERS_MIRROR', expected_history_mode='sql')
    def read(pod, container, script):
        assert container == 'destination-loader'
        compile(script, 'sql-history-probe', 'exec')
        assert 'StreamingIngestClient' not in script and 'offset_token' not in script
        assert 'QUADRINGENT_HISTORY_MODE' in script
        assert 'STATEMENT_TIMEOUT_IN_SECONDS = 10' in script
        return {'history_mode': actual_mode, 'history': [], 'mirror': []}
    probe.exec_json = Mock(side_effect=read)
    workload = ({'pod': 'pod', 'container': 'destination-loader'},
                {'QUADRINGENT_DESTINATION_DATABASE': 'DB', 'QUADRINGENT_DESTINATION_SCHEMA': 'SCHEMA'})
    if actual_mode == 'sql':
        assert probe.snowflake(workload)['history_mode'] == 'sql'
    else:
        with pytest.raises(ValueError):
            probe.snowflake(workload)


@pytest.mark.parametrize('baseline_mode', ['streaming', None])
def test_sql_observer_refuses_foreign_baseline_before_network(tmp_path, baseline_mode):
    from argparse import Namespace
    from unittest.mock import Mock
    data = sql_fixture(tmp_path)
    data[0]['history_mode'] = baseline_mode
    fixtures.private(data[5], data[0])
    probe = object.__new__(proof.NativeProbe)
    probe.args = Namespace(receipt=str(data[4][0]), baseline=str(data[5]), expected_history_mode='sql')
    probe.workload = Mock(side_effect=AssertionError('no network'))
    with pytest.raises(ValueError):
        probe.observe()
    probe.workload.assert_not_called()
