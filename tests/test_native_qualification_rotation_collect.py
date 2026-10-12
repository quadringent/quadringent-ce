"""Chaînes et segments synthétiques ; aucune connexion IBM i."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import pytest
from quadringent.qualification import collect


def fixture():
    args=SimpleNamespace(journal_library='S',journal_name='J',pipeline='p1',table='S.T',table_id='t1',namespace='dev',context='site',capture_digest='sha256:'+'b'*64)
    boundary={'receiver_name':'R1','receiver_library':'S','last_sequence':10}
    checkpoint={'receiver':'R2','sequence':15}
    catalog={'kind':'ibmi-source-receiver-catalog',**{k:getattr(args,k) for k in collect.SCOPE},'journal_library':'S','journal_name':'J',
             'observed_utc':'2026-10-05T00:01:00Z','output':'as400-receiver-catalog-v2\nreceiver\tS\tR1\tONLINE\t1\t12\t-\t-\tS\tR2\nreceiver\tS\tR2\tATTACHED\t13\t15\tS\tR1\t-\t-\n'}
    catalog.update(output_sha256=hashlib.sha256(catalog['output'].encode()).hexdigest(),jar_sha256='a'*64,reader={'uid':'deployment','pod_uid':'pod','image_binding':{'verified':True,'index':args.capture_digest,'observed_digest':args.capture_digest}})
    return args,boundary,checkpoint,catalog


def test_authenticated_rotation_windows_are_complete_and_bounded():
    args,boundary,checkpoint,catalog=fixture()
    assert collect.receiver_windows(boundary,checkpoint,catalog,args)==[
        {'receiver':'R1','receiver_library':'S','start':11,'end':12},
        {'receiver':'R2','receiver_library':'S','start':13,'end':15}]


@pytest.mark.parametrize('change',['foreign','missing','duplicate','gap','reset','order','bounds','bool','budget','checkpoint','detached','truncated'])
def test_rotation_chain_refuses_ambiguous_or_incomplete_catalog(change):
    args,boundary,checkpoint,catalog=fixture();lines=catalog['output'].splitlines()
    if change=='foreign':catalog['journal_library']='FOREIGN'
    elif change=='missing':catalog['output']=''
    elif change=='duplicate':catalog['output']=catalog['output'].replace('R2','R1')
    elif change=='gap':catalog['output']=catalog['output'].replace('13','14')
    elif change=='reset':catalog['output']=catalog['output'].replace('13','1')
    elif change=='order':catalog['output']='\n'.join([lines[0],lines[2],lines[1]])+'\n'
    elif change=='bounds':catalog['output']=catalog['output'].replace('\t12','\t-')
    elif change=='bool':catalog['output']=catalog['output'].replace('13','True')
    elif change=='budget':catalog['output']=catalog['output'].replace('15','1015');checkpoint['sequence']=1015
    elif change=='checkpoint':checkpoint['receiver']='unobserved'
    elif change=='detached':catalog['output']=catalog['output'].replace('ATTACHED','ONLINE')
    else:catalog['output']=lines[0]+'\n'+''.join(f'receiver\tS\tR{i}\tONLINE\t{i*10}\t{i*10+9}\t-\t-\t-\t-\n' for i in range(1,10))
    if change not in ('foreign','checkpoint'):catalog['output_sha256']=hashlib.sha256(catalog['output'].encode()).hexdigest()
    with pytest.raises(ValueError):collect.receiver_windows(boundary,checkpoint,catalog,args)


def oracle_fixture():
    import hashlib
    import test_native_qualification_collect as fixtures
    copy,old,positions,_,args,columns=fixtures.OracleCollectorTests().fixture()
    _,_,checkpoint,catalog=fixture();args.capture_digest='sha256:'+'b'*64
    new=deepcopy(old['records'][0]);new.update(journal_receiver='R2',journal_sequence=13,event_id=hashlib.sha256(b'ibmi|J|R2|13').hexdigest());new['after']['ID']=13
    journal={'kind':'ibmi-multi-receiver-journal','receiver_catalog':catalog,'segments':[
        {**old,'receiver_library':'S','end':12},
        {'receiver':'R2','receiver_library':'S','start':13,'end':15,'decoded':1,'scan_complete':True,'records':[new]}]}
    positions['positions'].append({'JOURNAL_RECEIVER_NAME':'R2','SEQUENCE_NUMBER':13})
    positions['receivers']=[{'JOURNAL_RECEIVER_LIBRARY':'S','JOURNAL_RECEIVER_NAME':name} for name in ('R1','R2')]
    return copy,journal,positions,checkpoint,args,columns


def test_multi_receiver_oracle_compares_all_independent_images_and_rowpos():
    result=collect.validate_oracle_parts(*oracle_fixture())
    assert len(result['events'])==3
    assert result['journal_scan']['scan_complete'] is True
    assert len(result['journal_scan']['segments'])==2
    assert result['native_cdc_qualified'] is False


@pytest.mark.parametrize('change',['segment-omitted','segment-order','segment-scope','partial','missing-image','extra-image','rowpos-foreign','chain-tamper'])
def test_multi_receiver_oracle_refuses_incomplete_or_tampered_evidence(change):
    data=oracle_fixture();journal=data[1];positions=data[2]
    if change=='segment-omitted':journal['segments'].pop()
    elif change=='segment-order':journal['segments'].reverse()
    elif change=='segment-scope':journal['segments'][1]['receiver_library']='OTHER'
    elif change=='partial':journal['segments'][0]['scan_complete']=False
    elif change=='missing-image':journal['segments'][1]['records']=[];journal['segments'][1]['decoded']=0
    elif change=='extra-image':journal['segments'][1]['records']*=2;journal['segments'][1]['decoded']=2
    elif change=='rowpos-foreign':positions['positions'][1]['JOURNAL_RECEIVER_NAME']='OTHER'
    else:positions['receivers'].reverse()
    with pytest.raises(ValueError):collect.validate_oracle_parts(*data)


@pytest.mark.parametrize('change',['output-hash','jar','pod','index','observed-digest'])
def test_catalog_requires_original_raw_and_actual_runtime_provenance(change):
    args,boundary,checkpoint,catalog=fixture()
    if change=='output-hash':catalog['output_sha256']='0'*64
    elif change=='jar':catalog['jar_sha256']='not-a-hash'
    elif change=='pod':catalog['reader']['pod_uid']=''
    elif change=='index':catalog['reader']['image_binding']['index']='sha256:'+'c'*64
    else:catalog['reader']['image_binding']['observed_digest']='invalid'
    with pytest.raises(ValueError):collect.receiver_windows(boundary,checkpoint,catalog,args)


def test_live_collector_reads_each_authenticated_receiver_window_once(monkeypatch):
    args,boundary,checkpoint,catalog=fixture()
    collector=collect.LiveCollector(SimpleNamespace(args=args))
    calls=[]
    monkeypatch.setattr(collector,'receiver_catalog',lambda reader,copy:catalog)
    def read(reader,copy,checkpoint):
        b=copy['boundary'];calls.append((b['receiver_name'],b['last_sequence']+1,checkpoint['sequence']))
        return {'receiver':b['receiver_name'],'start':b['last_sequence']+1,'end':checkpoint['sequence'],'decoded':0,'scan_complete':True,'records':[],'raw_batches':[]}
    monkeypatch.setattr(collector,'_journal_single',read)
    result=collector.journal([{}],{'boundary':boundary},checkpoint)
    assert calls==[('R1',11,12),('R2',13,15)]
    assert result['receiver_catalog']['output']==catalog['output']
    assert len(result['segments'])==2


def test_catalog_transport_reuses_public_readonly_catalog_without_timestamp_invention():
    args,boundary,_,catalog=fixture();scripts=[]
    def execute(pod,container,script):
        compile(script,'readonly-catalog','exec');scripts.append(script);return deepcopy(catalog)
    probe=SimpleNamespace(args=args,exec_json=execute)
    collector=collect.LiveCollector(probe)
    reader={**catalog['reader'],'pod':'actual-reader','container':'reader'}
    result=collector.receiver_catalog([reader],{'boundary':boundary})
    assert result['reader']==reader
    assert 'ReadOnlyReceiverCatalog' in scripts[0]
    assert "AS400_RECEIVER_METADATA_LIMIT='9'" in scripts[0]
    assert 'ATTACH_TIMESTAMP' not in scripts[0]
    assert "Path('/app/probe.jar').read_bytes()" in scripts[0]


def retained_multi_fixture(tmp_path):
    import base64,json
    from quadringent.contract import ChangeEvent,JournalPosition
    from quadringent.raw import RawBatchWriter
    copy,journal,positions,checkpoint,args,columns=oracle_fixture()
    for index,segment in enumerate(journal['segments']):
        old=segment['records'][0]
        event=ChangeEvent('ibmi','J','S','T','c',JournalPosition(segment['receiver'],old['journal_sequence']),
                          '2026-10-05T00:00:00Z','schema',None,old['after'])
        segment['records']=[event.to_record()]
        directory=tmp_path/str(index);writer=RawBatchWriter(directory)
        manifest=writer.write_batch([event],high_watermark=JournalPosition(segment['receiver'],segment['end']))
        segment['raw_batches']=[{'payload_b64':base64.b64encode((directory/('batch-'+manifest.batch_id+'.jsonl')).read_bytes()).decode(),
            'manifest_b64':base64.b64encode((directory/('batch-'+manifest.batch_id+'.manifest.json')).read_bytes()).decode()}]
    lines=['SRC_RECEIVER='+json.dumps(r) for r in positions['receivers']]
    lines+=['SRC_ROWPOS='+json.dumps(r) for r in positions['positions']]
    lines+=['SRC_ROWPOS_COUNT='+str(len(positions['positions']))]
    positions['rowpos_b64']=base64.b64encode(('\n'.join(lines)+'\n').encode()).decode()
    return dict(copy=copy,journal=journal,positions=positions,checkpoint=checkpoint),args,columns


def test_retained_rotation_redecodes_original_bytes_and_catalog(tmp_path):
    parts,args,columns=retained_multi_fixture(tmp_path)
    result=collect.validate_retained_parts(parts,args,columns)
    assert len(result['events'])==3
    assert result['journal_scan']['receiver_catalog']['output']==parts['journal']['receiver_catalog']['output']


@pytest.mark.parametrize('change',['payload','manifest','rowpos','catalog','segment-omitted'])
def test_retained_rotation_rejects_modified_or_omitted_original_bytes(tmp_path,change):
    import base64
    parts,args,columns=retained_multi_fixture(tmp_path)
    if change=='payload':parts['journal']['segments'][1]['raw_batches'][0]['payload_b64']=base64.b64encode(b'{}\n').decode()
    elif change=='manifest':parts['journal']['segments'][1]['raw_batches'][0]['manifest_b64']=base64.b64encode(b'{}').decode()
    elif change=='rowpos':parts['positions']['rowpos_b64']=base64.b64encode(b'SRC_ROWPOS_COUNT=0\n').decode()
    elif change=='catalog':parts['journal']['receiver_catalog']['output']+='receiver\tS\tFOREIGN\tONLINE\t16\t20\n'
    else:parts['journal']['segments'][1]['raw_batches']=[]
    with pytest.raises((ValueError,KeyError)):collect.validate_retained_parts(parts,args,columns)
