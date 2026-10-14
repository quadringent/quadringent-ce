"""Le fichier loader ne doit pas masquer le package control-plane installé."""
import json
import subprocess
import sys

import pytest

from quadringent.qualification.proof import NativeProbe


def test_loader_file_preserves_installed_package_and_dataclasses(tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'quadringent_control_plane.py').write_text("raise RuntimeError('shim CLI masquant le package')\n")
    package = tmp_path / 'installed' / 'quadringent_control_plane' / 'v2'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    (package / 'evidence.py').write_text('VALUE=42\n')
    loader = app / 'quadringent_destination_loader.py'
    loader.write_text('from dataclasses import dataclass\nfrom quadringent_control_plane.v2.evidence import VALUE\n@dataclass\nclass Table:\n value:int=VALUE\n')
    script = NativeProbe.loader_import_script('loader').replace('/app/quadringent_destination_loader.py', str(loader))
    script = f'import sys;sys.path.insert(0,{str(package.parents[1])!r})\n' + script
    script += "assert loader.Table().value==42\nassert " + repr(str(app)) + " not in sys.path\n"
    result = subprocess.run([sys.executable, '-P', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_loader_import_missing_file_fails_closed(tmp_path):
    script = NativeProbe.loader_import_script('l').replace('/app/quadringent_destination_loader.py', str(tmp_path / 'missing.py'))
    result = subprocess.run([sys.executable, '-P', '-c', script], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'FileNotFoundError' in result.stderr


def test_identity_scripts_keep_exact_cloud_and_copy_predicates():
    from argparse import Namespace
    probe = NativeProbe.__new__(NativeProbe)
    probe.args = Namespace(expected_gcp_service_account='runtime@example.test', expected_gcp_project='project',
                           expected_gcs_bucket='bucket', expected_gcp_instance_id='123', expected_gcp_instance_name='vm',
                           expected_gcp_zone='zone', expected_gcp_machine_type='machine', table_id='table',
                           gcs_read_key='copy/evidence.json', pipeline='pipeline', copy_run_id='run', copy_boundary='R:10')
    for script in (probe.gke_identity_script(), probe.gcp_vm_identity_script()):
        compile(script, '<identity-probe>', 'exec')
        assert "sys.path.insert" not in script
        assert "credentials.refresh(request)" in script
        assert "type(credentials) is not metadata_credentials.Credentials" in script
        assert "tables[0].evidence_key!='copy/evidence.json'" in script
        assert "blob.download_as_bytes(if_generation_match=blob.generation,timeout=10,retry=None)" in script
        assert "'R:10'" in script
    assert "instance['id']!='123'" in probe.gcp_vm_identity_script()


@pytest.mark.parametrize('method', ('gke_identity_script', 'gcp_vm_identity_script'))
def test_identity_scripts_reject_copy_from_another_run(monkeypatch, capsys, method):
    from test_native_qualification_bound_boundary import gcp_fixture, gcs_copy

    evidence, binding = gcs_copy()
    probe, _ = gcp_fixture(monkeypatch, evidence, binding)
    probe.args.expected_gcp_instance_id = '123'
    probe.args.expected_gcp_instance_name = 'vm'
    probe.args.expected_gcp_zone = 'zone'
    probe.args.expected_gcp_machine_type = 'machine'
    metadata = sys.modules['google.auth.compute_engine._metadata']
    info = {'email': probe.args.expected_gcp_service_account,
            'scopes': ['https://www.googleapis.com/auth/cloud-platform']}
    instance = {'instance/id': '123', 'instance/name': 'vm',
                'instance/zone': 'projects/project/zones/zone',
                'instance/machine-type': 'projects/project/machineTypes/machine'}
    monkeypatch.setattr(metadata, 'get_service_account_info', lambda *a, **kw: info, raising=False)
    monkeypatch.setattr(metadata, 'get', lambda request, key: instance[key], raising=False)

    exec(compile(getattr(probe, method)(), '<identity-probe>', 'exec'), {})
    result = json.loads(capsys.readouterr().out)
    assert result['service_account_email'] == probe.args.expected_gcp_service_account
    assert result['gcs_read']['verified'] is True
    assert result['gcs_read']['key'] == probe.args.gcs_read_key
    probe.args.copy_run_id = 'foreign-run'
    with pytest.raises(ValueError, match='pipeline/table/run'):
        exec(compile(getattr(probe, method)(), '<identity-probe>', 'exec'), {})
    assert capsys.readouterr().out == ''


def test_live_collector_scripts_use_installed_packages_and_exact_loader():
    from copy import deepcopy
    from quadringent.qualification.collect import LiveCollector
    from test_native_qualification_bound_boundary import fixture

    evidence, args, binding = fixture()
    probe = NativeProbe.__new__(NativeProbe)
    probe.args = args
    args.table, args.database, args.schema = 'S.T', 'DB', 'S'
    args.pk, args.history, args.mirror = 'ID', 'H', 'M'
    probe.library = 'S'
    probe.table = 'T'
    probe.columns = {'ID': 'int'}
    scripts = []
    probe.exec_json = lambda pod, container, script: scripts.append(script)
    collector = LiveCollector(probe)
    collector.copy(({'pod': 'loader', 'container': 'loader'}, {}))
    checkpoint = {'receiver': 'R1', 'sequence': 11,
                  'bound_checkpoint': binding.checkpoint(binding.bootstrap.receiver_identity, 11).to_dict(),
                  'capture_binding': binding.to_dict()}
    reader = ({'pod': 'reader', 'container': 'reader'}, {})
    collector.journal(reader, evidence, checkpoint)
    for script in scripts:
        compile(script, '<live-collector>', 'exec')
        assert 'sys.path.insert' not in script
        assert 'from quadringent.raw import read_raw_batch' in script
    assert "'/app/quadringent_destination_loader.py'" in scripts[0]
    assert "evidence_key=t.evidence_key" in scripts[0]
    assert "get_bounded(evidence_key,262144)" in scripts[0]
    assert "batch.manifest.high_watermark.receiver!='SNAPSHOT:'+'run1'" in scripts[0]
    assert "summary.get('scan_complete')!='true'" in scripts[1]
    foreign = deepcopy(evidence)
    foreign['run_id'] = 'foreign-run'
    with pytest.raises(ValueError, match='pipeline/table/run'):
        collector.journal(reader, foreign, checkpoint)
    assert len(scripts) == 2


def test_live_copy_script_rejects_copy_from_another_run(tmp_path, monkeypatch, capsys):
    from test_native_qualification_bound_boundary import copy_script_fixture

    collector, table, stores, evidence, _, _, _ = copy_script_fixture(tmp_path, monkeypatch, capsys)
    evidence['run_id'] = 'foreign-run'
    stores[''].values[table.evidence_key] = json.dumps(evidence).encode()
    with pytest.raises(ValueError, match='pipeline/table/run'):
        collector.copy(({'pod': 'loader', 'container': 'destination-loader'}, {}))
    assert capsys.readouterr().out == ''


def test_runtime_interpreter_and_module_path_follow_product_images():
    probe = NativeProbe.__new__(NativeProbe)
    calls = []
    probe.kubectl = lambda *args, **kwargs: calls.append((args, kwargs))
    probe.exec_json('capture-pod', 'reader', 'import google.auth\nfrom quadringent.raw import read_raw_batch')
    probe.exec_json('loader-pod', 'destination-loader', NativeProbe.loader_import_script('l'))
    reader, loader = calls
    assert reader[0][-3:] == ('/opt/venv/bin/python', '-P', '-')
    assert reader[1]['stdin'].startswith("import sys;sys.path.append('/app')\n")
    assert loader[0][-3:] == ('python', '-P', '-')
    assert 'sys.path.append' not in loader[1]['stdin']
    assert 'sys.path.insert' not in reader[1]['stdin'] + loader[1]['stdin']
