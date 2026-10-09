"""Binding rotation hors réseau : ACK originaux et images de deux receivers."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import pytest
from quadringent.qualification import collect
from test_native_qualification_rotation import fixture


def inputs():
    before, after, ops = fixture()
    columns={'ID':'int','N':'decimal','LABEL':'text','D':'date','T':'timestamp','NULLABLE':'text','SPACE':'text'}
    values={'ID':1,'N':'1.20','LABEL':'rotation marker','D':'2026-09-30','T':'2026-09-30T10:00:00','NULLABLE':None,'SPACE':'x '}
    for snapshot in (before,after):
        snapshot['source']=[deepcopy(values)];snapshot['mirror']=[deepcopy(values)]
        for event in snapshot['history']:event.update(values)
    ub={**after['history'][-1],'OPERATION':'u_before','EVENT_ID':'ub','JOURNAL_SEQUENCE':11}
    up={**after['history'][-1],'OPERATION':'u_after','EVENT_ID':'up','JOURNAL_SEQUENCE':12}
    after['history']=deepcopy(before['history'])+[ub,up]
    after['checkpoint']['sequence']=12
    after['source_journal_positions']['positions']=[{'JOURNAL_RECEIVER_NAME':'R2','SEQUENCE_NUMBER':n} for n in (11,12)]
    ops['catalog_after']['output']=ops['catalog_after']['output'].replace('R2\tATTACHED\t11\t11','R2\tATTACHED\t11\t12')
    ops['catalog_after']['output_sha256']=hashlib.sha256(ops['catalog_after']['output'].encode()).hexdigest()
    scope={k:before[k] for k in collect.SCOPE}
    ack={k:v for k,v in ops['mutations'][0].items() if k!='expected_events'}
    ack.update(operation='update',mutation_identity=values)
    oracle={'kind':'ibmi-journal-and-snapshot-oracle',**scope,'independent_of_snowflake':True,
            'copy_run_id':'run','copy_boundary':'R1:10','events':deepcopy(after['history']),
            'checkpoint':deepcopy(after['checkpoint']),'observed_utc':after['observed_utc'],
            'source_journal_positions':deepcopy(after['source_journal_positions'])}
    args=SimpleNamespace(**scope,columns=columns,pk='ID',copy_run_id='run',copy_boundary='R1:10',journal_library='S',journal_name='J')
    return [ack],oracle,ops['rotation'],before,after,ops['catalog_before'],ops['catalog_after'],args


def test_rotation_binds_new_receiver_without_rewriting_before():
    data=inputs();original=deepcopy(data[3])
    result=collect.bind_rotation_mutation_receipts(*data)
    assert result[0]['source_positions']==[{'receiver':'R2','sequence':11},{'receiver':'R2','sequence':12}]
    assert result[0]['expected_events']==data[1]['events'][-2:]
    assert data[3]==original


@pytest.mark.parametrize('change',[
 lambda x:x[0][0].update(write_started_utc=x[2]['write_ack_utc']),
 lambda x:x[0][0].update(source_positions=[{'receiver':'R1','sequence':11}]),
 lambda x:x[0][0].update(table_id='foreign'),
 lambda x:x[1].update(independent_of_snowflake=False),
 lambda x:x[1].update(copy_run_id='foreign'),
 lambda x:x[1]['events'][0].update(LABEL='tampered'),
 lambda x:x[4]['history'][0].update(LABEL='tampered'),
 lambda x:x[6].update(output=x[5]['output']),
 lambda x:x[2].update(sequence_option='RESET'),
 lambda x:x[0][0]['mutation_identity'].pop('SPACE'),
 lambda x:x[1]['source_journal_positions']['positions'].clear(),
])
def test_rotation_binding_refuses_false_or_incomplete_evidence(change):
    data=inputs();change(data)
    for c in (data[5],data[6]):c['output_sha256']=hashlib.sha256(c['output'].encode()).hexdigest()
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)


def test_rotation_accepts_positions_really_bound_to_oracle():
    data=inputs();data[0][0]['source_positions']=[{'receiver':'R2','sequence':11},{'receiver':'R2','sequence':12}]
    assert collect.bind_rotation_mutation_receipts(*data)[0]['expected_events']==data[1]['events'][-2:]


@pytest.mark.parametrize('change',[
 lambda x:x[0][0].update(write_ack_utc=x[4]['observed_utc']),
 lambda x:x[1]['events'][-1].update(JOURNAL_RECEIVER='FOREIGN'),
 lambda x:x[1]['events'][-2].update(OPERATION='c'),
 lambda x:x[1]['events'].append(deepcopy(x[1]['events'][-1])),
 lambda x:x[6].update(output=x[6]['output'].replace('R2\tATTACHED\t11','R2\tATTACHED\t1')),
 lambda x:x[0][0].update(expected_events=[]),
])
def test_rotation_binding_rejects_boundary_pairs_or_declared_images(change):
    data=inputs();change(data)
    data[6]['output_sha256']=hashlib.sha256(data[6]['output'].encode()).hexdigest()
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)


def test_rotation_binding_rejects_missing_nullable_image_even_for_null_ack():
    data=inputs()
    data[1]['events'][-1].pop('NULLABLE');data[4]['history'][-1].pop('NULLABLE')
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)


@pytest.mark.parametrize('rows',[[],[{'ID':999,'N':'1.20','LABEL':'rotation marker','D':'2026-09-30','T':'2026-09-30T10:00:00','NULLABLE':None,'SPACE':'x '}]])
def test_rotation_rejects_source_and_mirror_agreeing_on_wrong_update_result(rows):
    data=inputs();data[4]['source']=deepcopy(rows);data[4]['mirror']=deepcopy(rows)
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)


def test_rotation_replays_changed_update_with_before_image_validation():
    data=inputs();before_values=deepcopy(data[3]['source'][0]);before_values['LABEL']='old label'
    data[3]['source']=[before_values];data[3]['mirror']=deepcopy(data[3]['source'])
    data[3]['history'][0]['LABEL']='old label'
    for rows in (data[1]['events'],data[4]['history']):
        rows[0]['LABEL']='old label';rows[-2]['LABEL']='old label'
    assert collect.bind_rotation_mutation_receipts(*data)
    for rows in (data[1]['events'],data[4]['history']):rows[-2]['LABEL']='false before'
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)


def test_typed_replay_insert_update_delete_in_real_journal_order():
    from quadringent.qualification import proof
    columns={'ID':'int','NULLABLE':'text'}
    def event(operation,sequence,identifier,value):
        return {'ID':identifier,'NULLABLE':value,'OPERATION':operation,'JOURNAL_SEQUENCE':sequence}
    mutations=[{'operation':'delete','expected_events':[event('d',4,1,'new')]},
               {'operation':'insert','expected_events':[event('c',1,2,None)]},
               {'operation':'update','expected_events':[event('u_before',2,1,None),event('u_after',3,1,'new')]}]
    proof.validate_mutation_result([{'ID':1,'NULLABLE':None}],[{'ID':2,'NULLABLE':None}],[{'ID':2,'NULLABLE':None}],mutations,columns,'ID')
    mutations[0]['expected_events'][0]['NULLABLE']='wrong'
    with pytest.raises(ValueError):proof.validate_mutation_result([{'ID':1,'NULLABLE':None}],[],[],mutations,columns,'ID')


@pytest.mark.parametrize('field',['source','mirror','both'])
def test_rotation_rejects_missing_nullable_in_final_business_rows(field):
    data=inputs()
    for name in ('source','mirror'):
        if field in (name,'both'):data[4][name][-1].pop('NULLABLE')
    with pytest.raises(ValueError):collect.bind_rotation_mutation_receipts(*data)
