"""Dispatch rotation synthétique ; aucune action source ou Kubernetes."""
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from quadringent.qualification import cli, collect


@pytest.mark.parametrize('history_mode', ['streaming', 'sql'])
def test_cli_rotation_is_separate_readonly_phase_and_preserves_historical_gate(tmp_path,monkeypatch,history_mode):
    scope=dict(pipeline='p',table='S.T',table_id='t',namespace='dev',context='site')
    before={**scope,'history_mode':history_mode,'reader':{'container':'reader'},'loader':{'container':'destination-loader'}}
    after=dict(before);payloads={'before':before,'after':after,'rotation_proof':{'actual_ack':'original'},
        'catalog_before':{'raw':'before'},'catalog_after':{'raw':'after'},'mutation_receipts':[{'actual_ack':'dml'}]}
    refs={}
    for name,value in payloads.items():
        path=tmp_path/(name+'.json');path.write_text(json.dumps(value));path.chmod(0o600);refs[name]=str(path)
    data={'release':{'mode':'conserved'},'native':{},'refs':refs}
    args=SimpleNamespace(**scope,max_seconds=120,poll_seconds=1,reader_deployment='reader',loader_deployment='loader',capture_digest='capture',loader_digest='loader',pk='ID',journal_library='S',journal_name='J',expected_history_mode=history_mode)
    monkeypatch.setattr(cli,'load_site',lambda *a,**k:data)
    data['release'].update(metadata_root=str(tmp_path),source_sha='a'*40)
    monkeypatch.setattr(cli,'release_proof',lambda *a:str(tmp_path/'admission'))
    monkeypatch.setattr(cli,'native_arguments',lambda *a:args)
    probe=SimpleNamespace(args=args,columns={'ID':'int'},workload=lambda name,digest:(after['reader' if name=='reader' else 'loader'],None))
    monkeypatch.setattr(cli.proof,'NativeProbe',lambda args:probe)
    monkeypatch.setattr(cli.collect,'LiveCollector',lambda probe:SimpleNamespace(probe=probe))
    inventory=Mock(return_value=[]);monkeypatch.setattr(cli,'history_inventory_gate',inventory)
    monkeypatch.setattr(cli,'retained_manifest',lambda native:None)
    monkeypatch.setattr(cli.proof,'workload_candidate',lambda proof,container:container)
    def evaluate(b,a,operations):
        assert operations['rotation']==payloads['rotation_proof']
        assert operations['catalog_before']==payloads['catalog_before']
        assert operations['catalog_after']==payloads['catalog_after']
        assert operations['columns']=={'ID':'int'} and operations['pk']=='ID'
        assert operations['journal_library']=='S' and operations['journal_name']=='J'
        return {'rotation_qualified':True,'native_cdc_qualified':False,'maximum_10s_qualified':False}
    monkeypatch.setattr(cli.proof,'evaluate_rotation',evaluate)
    action=Mock(side_effect=AssertionError('no mutation'));monkeypatch.setattr(cli,'native_action',action)
    output=StringIO()
    assert cli.run_native('unused',str(tmp_path/'out'),'evaluate-rotation',stdout=output)==0
    inventory.assert_called_once_with(probe,None);action.assert_not_called()
    assert json.loads(output.getvalue())['native_cdc_qualified'] is False
    assert json.loads((tmp_path/'out'/'evaluate-rotation.json').read_text())['history_mode']==history_mode
    assert 'receiver-catalog' in cli.PHASES
    assert 'evaluate-rotation' in cli.HISTORY_PHASES
    assert collect.SCOPE==tuple(scope)


def test_cli_rotation_binding_dispatches_only_real_references(tmp_path,monkeypatch):
    from test_native_qualification_rotation_binding import inputs
    acks,oracle,rotation,before,after,cb,ca,args=inputs()
    payloads={'ack_receipts':acks,'history_oracle':oracle,'rotation_proof':rotation,'before':before,'after':after,'catalog_before':cb,'catalog_after':ca}
    refs={}
    for name,value in payloads.items():
        path=tmp_path/(name+'.json');path.write_text(json.dumps(value));path.chmod(0o600);refs[name]=str(path)
    data={'release':{'mode':'conserved','metadata_root':str(tmp_path),'source_sha':'a'*40},'native':{},'refs':refs}
    args.max_seconds=120;args.expected_history_mode='sql'
    monkeypatch.setattr(cli,'load_site',lambda *a,**k:data)
    monkeypatch.setattr(cli,'release_proof',lambda *a:str(tmp_path/'admission'))
    monkeypatch.setattr(cli,'native_arguments',lambda *a:args)
    monkeypatch.setattr(cli.proof,'NativeProbe',lambda args:SimpleNamespace(args=args))
    monkeypatch.setattr(cli.collect,'LiveCollector',lambda probe:None)
    monkeypatch.setattr(cli,'retained_manifest',lambda native:None)
    action=Mock(side_effect=AssertionError('no mutation'));monkeypatch.setattr(cli,'native_action',action)
    output=StringIO()
    assert cli.run_native('unused',str(tmp_path/'out'),'bind-rotation-mutations',stdout=output)==0
    result=json.loads((tmp_path/'out'/'bind-rotation-mutations.json').read_text())
    assert result[0]['expected_events']==oracle['events'][-2:]
    action.assert_not_called()
    assert 'bind-rotation-mutations' in cli.PHASES
