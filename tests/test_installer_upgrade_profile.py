"""Une réinstallation conserve le profil effectif et refuse une lecture ambiguë."""
import io
import json

import pytest
import yaml

from quadringent.installer.apply import _deploy_chart
from quadringent.installer.plan import InstallInputs
from quadringent.installer.runner import CommandResult, RecordingRunner
from test_installer_apply import MANIFEST

LIST = ('helm', 'list', '--namespace', 'quadringent', '--filter', '^demo-int$', '--all', '--output', 'json')
GET = ('helm', 'get', 'values', 'demo-int', '--namespace', 'quadringent', '--all', '--output', 'json')
LIST_V4 = tuple(value for value in LIST if value != '--all')


def deploy(tmp_path, scripted):
    runner = RecordingRunner(scripted_results=scripted)
    inputs = InstallInputs(cloud='aws', target='vm', region='eu-west-3', name='demo-int')
    code = _deploy_chart(inputs, MANIFEST, tmp_path, runner, io.StringIO(),
                         {'runtime_role_arn': 'arn:aws:iam::000000000000:role/test'}, {},
                         '000000000000', None)
    return code, runner


@pytest.mark.parametrize('mode,polls,flush', [('streaming', (5, 10), True), ('sql', (2, 3), False)])
def test_upgrade_preserves_effective_profile_before_template(tmp_path, mode, polls, flush):
    effective = {'loaderHistoryMode': mode, 'readerPollSeconds': polls[0],
                 'loaderPollSeconds': polls[1], 'loaderFlushEachBatch': flush}
    code, runner = deploy(tmp_path, {LIST: CommandResult(LIST, 0, '[{"name":"demo-int"}]'),
                                    GET: CommandResult(GET, 0, json.dumps({'controlPlane': {'v2': effective}}))})
    assert code == 0
    values = yaml.safe_load((tmp_path/'chart-values.generated.yaml').read_text())
    assert {key: values['controlPlane']['v2'][key] for key in effective} == effective
    commands = [call.argv for call in runner.calls]
    assert commands.index(GET) < next(i for i, command in enumerate(commands) if command[:2] == ('helm', 'template'))


@pytest.mark.parametrize('response', [CommandResult(LIST, 1, '', 'forbidden'), CommandResult(LIST, 0, ''),
                                     CommandResult(LIST, 0, '{}')])
def test_unknown_existing_release_state_refuses_upgrade(tmp_path, response):
    code, runner = deploy(tmp_path, {LIST: response})
    assert code == 1
    assert not any(call.argv[:2] == ('helm', 'upgrade') for call in runner.calls)
    assert not (tmp_path/'chart-values.generated.yaml').exists()


def test_existing_release_missing_mode_refuses_implicit_migration(tmp_path):
    code, runner = deploy(tmp_path, {LIST: CommandResult(LIST, 0, '[{"name":"demo-int"}]'),
                                    GET: CommandResult(GET, 0, '{"controlPlane":{"v2":{}}}')})
    assert code == 1
    assert not any(call.argv[:2] == ('helm', 'upgrade') for call in runner.calls)


def test_helm4_list_preserves_existing_profile(tmp_path):
    profile = {'loaderHistoryMode': 'sql', 'readerPollSeconds': 2,
               'loaderPollSeconds': 3, 'loaderFlushEachBatch': False}
    code, runner = deploy(tmp_path, {
        LIST: CommandResult(LIST, 1, '', 'Error: unknown flag: --all\n'),
        LIST_V4: CommandResult(LIST_V4, 0, '[{"name":"demo-int"}]'),
        GET: CommandResult(GET, 0, json.dumps({'controlPlane': {'v2': profile}})),
    })
    assert code == 0
    values = yaml.safe_load((tmp_path/'chart-values.generated.yaml').read_text())
    assert {key: values['controlPlane']['v2'][key] for key in profile} == profile
    assert [call.argv for call in runner.calls][:2] == [LIST, LIST_V4]


@pytest.mark.parametrize('message', ['forbidden', 'connection refused', 'unknown flag: --filter'])
def test_release_read_failures_do_not_trigger_helm4_retry(tmp_path, message):
    code, runner = deploy(tmp_path, {LIST: CommandResult(LIST, 1, '', message)})
    assert code == 1
    assert LIST_V4 not in [call.argv for call in runner.calls]


def test_helm4_missing_profile_still_refuses_upgrade(tmp_path):
    code, runner = deploy(tmp_path, {
        LIST: CommandResult(LIST, 1, '', 'Error: unknown flag: --all\n'),
        LIST_V4: CommandResult(LIST_V4, 0, '[{"name":"demo-int"}]'),
        GET: CommandResult(GET, 0, '{"controlPlane":{"v2":{}}}'),
    })
    assert code == 1
    assert not any(call.argv[:2] == ('helm', 'upgrade') for call in runner.calls)


@pytest.mark.parametrize('profile', [
    {'loaderHistoryMode': [], 'readerPollSeconds': 5, 'loaderPollSeconds': 10, 'loaderFlushEachBatch': False},
    {'loaderHistoryMode': 'streaming', 'readerPollSeconds': True, 'loaderPollSeconds': 10, 'loaderFlushEachBatch': False},
])
def test_invalid_effective_profile_refuses_upgrade(tmp_path, profile):
    code, runner = deploy(tmp_path, {LIST: CommandResult(LIST, 0, '[{"name":"demo-int"}]'),
                                    GET: CommandResult(GET, 0, json.dumps({'controlPlane': {'v2': profile}}))})
    assert code == 1
    assert not any(call.argv[:2] == ('helm', 'upgrade') for call in runner.calls)
