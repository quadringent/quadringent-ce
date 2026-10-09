"""Preuves synthétiques, aucun appel cloud/IBM i/Snowflake."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadringent.qualification import collect
import test_native_qualification_retained as fixtures


def archive_fixture(tmp_path):
    parts,args,_,_=fixtures.fixture('old')
    args.initial_copy_revision='historical-sha';args.expected_revision='current-sha'
    key=parts['copy']['artifacts'][0]['evidence_key']
    job=fixtures.job(args,key);job.update(apiVersion='batch/v1',kind='Job')
    job['metadata']['namespace']=args.namespace
    job['status']['completionTime']='2026-10-04T20:01:00Z';job['spec']['ttlSecondsAfterFinished']=3600
    original=tmp_path/'original.json';original.write_text(json.dumps({'kind':'List','items':[job]}));original.chmod(0o600)
    def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    anchor=tmp_path/'original-observation.json';anchor.write_text(json.dumps({'observed_utc':'2026-10-04T20:02:00Z','original_sha256':digest(original)}));anchor.chmod(0o600)
    document={'kind':'quadringent-job-api-archive-v1','scope':{k:getattr(args,k) for k in collect.SCOPE},'job_uid':job['metadata']['uid'],
              'api_document':{'path':str(original),'sha256':digest(original)},'observation_anchor':{'path':str(anchor),'sha256':digest(anchor)},
              'observed_utc_pointer':['observed_utc'],'api_sha256_pointer':['original_sha256']}
    envelope=tmp_path/'archive.json';envelope.write_text(json.dumps(document));envelope.chmod(0o600)
    args.initial_copy_job_archive={'path':str(envelope),'sha256':digest(envelope)}
    probe=SimpleNamespace(args=args,initial_copy_job_api=lambda name:{'kind':'quadringent-job-api-not-found','name':name,'namespace':args.namespace,'observed_utc':'2026-10-04T22:00:00Z'})
    return args,key,job,probe,document,envelope,digest


def test_historical_archive_after_actual_notfound_is_explicit(tmp_path):
    args,key,_,probe,*_=archive_fixture(tmp_path)
    evidence=collect.resolve_initial_copy_job([],args,key,probe)
    assert evidence['job_evidence_source']=='archived_api'
    assert evidence['uid']=='job-old'
    assert evidence['api_absence']['kind']=='quadringent-job-api-not-found'


@pytest.mark.parametrize('change',['sha','anchor-sha','anchor-hash','anchor-time','scope','uid','image','completion','ttl','failed','duplicate','boundary'])
def test_archive_refuses_modified_or_unbound_evidence(tmp_path,change):
    args,key,job,probe,doc,envelope,digest=archive_fixture(tmp_path)
    original=Path(doc['api_document']['path']);anchor=Path(doc['observation_anchor']['path'])
    if change=='sha':args.initial_copy_job_archive['sha256']='0'*64
    elif change=='anchor-sha':doc['observation_anchor']['sha256']='0'*64
    elif change=='scope':doc['scope']['context']='foreign'
    elif change=='uid':doc['job_uid']='foreign'
    elif change in {'anchor-hash','anchor-time'}:
        a=json.loads(anchor.read_text());a['original_sha256']='0'*64 if change=='anchor-hash' else a['original_sha256']
        if change=='anchor-time':a['observed_utc']='2026-10-04T21:02:00Z'
        anchor.write_text(json.dumps(a));doc['observation_anchor']['sha256']=digest(anchor)
    else:
        if change=='image':job['spec']['template']['spec']['containers'][0]['image']='runtime@sha256:'+'e'*64
        elif change=='completion':job['status']['completionTime']='2026-10-04T20:03:00Z'
        elif change=='ttl':job['spec']['ttlSecondsAfterFinished']=0
        elif change=='failed':job['status']['failed']=1
        elif change=='boundary':job['metadata']['annotations']['quadringent.io/bootstrap-boundary']='R1:999'
        original.write_text(json.dumps({'kind':'List','items':[job,deepcopy(job)] if change=='duplicate' else [job]}))
        doc['api_document']['sha256']=digest(original)
        a=json.loads(anchor.read_text());a['original_sha256']=digest(original);anchor.write_text(json.dumps(a));doc['observation_anchor']['sha256']=digest(anchor)
    envelope.write_text(json.dumps(doc))
    if change!='sha':args.initial_copy_job_archive['sha256']=digest(envelope)
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


def test_current_job_requires_current_reader_for_archive(tmp_path):
    args,key,_,probe,*_=archive_fixture(tmp_path);args.initial_copy_revision=args.expected_revision
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


def test_forbidden_is_never_absence(tmp_path):
    args,key,_,probe,*_=archive_fixture(tmp_path)
    def forbidden(name):raise RuntimeError('native_kubernetes_probe_failed')
    probe.initial_copy_job_api=forbidden
    with pytest.raises(RuntimeError):collect.resolve_initial_copy_job([],args,key,probe)


def test_live_job_conflict_refuses_archive(tmp_path):
    args,key,job,probe,*_=archive_fixture(tmp_path);other=deepcopy(job);other['metadata']['uid']='new-foreign-uid'
    probe.initial_copy_job_api=lambda name:other
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([other],args,key,probe)


def test_notfound_before_ttl_not_accepted(tmp_path):
    args,key,_,probe,*_=archive_fixture(tmp_path)
    probe.initial_copy_job_api=lambda name:{'kind':'quadringent-job-api-not-found','name':name,'namespace':args.namespace,'observed_utc':'2026-10-04T20:30:00Z'}
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


def test_archive_inventory_cannot_hide_other_successful_run(tmp_path):
    args,key,job,probe,doc,envelope,digest=archive_fixture(tmp_path)
    extra=deepcopy(job);extra['metadata']['uid']='extra-uid';extra['metadata']['annotations']['quadringent.io/run-id']='omitted-run'
    original=Path(doc['api_document']['path']);original.write_text(json.dumps({'kind':'List','items':[job,extra]}));doc['api_document']['sha256']=digest(original)
    anchor=Path(doc['observation_anchor']['path']);anchor.write_text(json.dumps({'observed_utc':'2026-10-04T20:02:00Z','original_sha256':digest(original)}));doc['observation_anchor']['sha256']=digest(anchor)
    envelope.write_text(json.dumps(doc));args.initial_copy_job_archive['sha256']=digest(envelope)
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


def test_inventory_requires_current_live_and_all_known_epochs():
    _,args,_,_=fixtures.fixture('new');j=fixtures.job(args,'key')
    collect.inventory_matches([j],args,['old','new'],['old'])
    with pytest.raises(ValueError):collect.inventory_matches([],args,['old','new'],['old'])
    extra=deepcopy(j);extra['metadata']['annotations']['quadringent.io/run-id']='extra'
    with pytest.raises(ValueError):collect.inventory_matches([j,extra],args,['old','new'],['old'])


@pytest.mark.parametrize('response',['forbidden','timeout','foreign-namespace','notfound','wrong-name'])
def test_exact_api_notfound_never_masks_permission_or_namespace(response,monkeypatch):
    from quadringent.qualification import proof
    probe=object.__new__(proof.NativeProbe)
    probe.args=SimpleNamespace(kubeconfig='kubeconfig',context='context',namespace='test',reader_deployment='reader')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if 'deployment' in argv:
            return SimpleNamespace(returncode=0,stdout=json.dumps({'kind':'Deployment','metadata':{'uid':'reader-uid','name':'reader','namespace':'foreign' if response=='foreign-namespace' else 'test'}}))
        if response=='timeout':raise TimeoutError('timeout')
        if response=='forbidden':return SimpleNamespace(returncode=1,stdout='',stderr='Forbidden')
        if response=='wrong-name':return SimpleNamespace(returncode=0,stdout=json.dumps({'kind':'Job','metadata':{'name':'other','namespace':'test'}}))
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(proof.subprocess,'run',run)
    if response=='notfound':
        result=probe.initial_copy_job_api('copy-job');assert result['kind']=='quadringent-job-api-not-found';assert result['namespace_witness']['uid']=='reader-uid'
    else:
        with pytest.raises((RuntimeError,TimeoutError,ValueError)):probe.initial_copy_job_api('copy-job')
    assert all('namespace' not in argv for argv in calls)  # Pas de permission cluster nouvelle.


def projection_fixture(tmp_path):
    args,key,job,probe,doc,envelope,digest=archive_fixture(tmp_path)
    parts,_,_,_=fixtures.fixture('old')
    del job['metadata']['namespace'];del job['spec']['ttlSecondsAfterFinished']
    original=Path(doc['api_document']['path'])
    original.write_text(json.dumps({'kind':'public-native-initial-copy-export','collector':'quadringent.qualification.collect.LiveCollector.copy','observed_utc':'2026-10-04T20:02:00Z','copy':parts['copy'],'job':job}))
    doc['api_document']['sha256']=digest(original)
    anchor=Path(doc['observation_anchor']['path'])
    anchor.write_text(json.dumps({'results':[{'files':{'initial-copy-export.json':{'sha256':digest(original)}}}]}))
    doc['observation_anchor']['sha256']=digest(anchor)
    doc['api_sha256_pointer']=['results',0,'files','initial-copy-export.json','sha256']
    doc.pop('observed_utc_pointer');doc['kind']='quadringent-job-api-projection-archive-v1'
    envelope.write_text(json.dumps(doc));args.initial_copy_job_archive['sha256']=digest(envelope)
    return args,key,probe,doc,envelope,digest


def test_projection_only_qualifies_captured_data_without_historical_scope_claims(tmp_path):
    args,key,probe,*_=projection_fixture(tmp_path)
    result=collect.resolve_initial_copy_job([],args,key,probe)
    assert result['job_evidence_source']=='archived_api_projection_limited'
    assert all(result[k] is False for k in collect.LIMITED_JOB_CLAIMS)


@pytest.mark.parametrize('change',['pointer-bool','pointer-string-index','anchor-sha','observed','run','boundary','current'])
def test_projection_fails_closed(tmp_path,change):
    args,key,probe,doc,envelope,digest=projection_fixture(tmp_path)
    original=Path(doc['api_document']['path'])
    if change=='pointer-bool':doc['api_sha256_pointer'][1]=False
    elif change=='pointer-string-index':doc['api_sha256_pointer'][1]='0'
    elif change=='anchor-sha':doc['observation_anchor']['sha256']='0'*64
    elif change=='current':args.initial_copy_revision=args.expected_revision
    else:
        data=json.loads(original.read_text())
        if change=='observed':data['observed_utc']='2026-10-04T20:00:00Z'
        elif change=='run':data['copy']['run_id']='other'
        else:data['copy']['boundary']['last_sequence']=999
        original.write_text(json.dumps(data));doc['api_document']['sha256']=digest(original)
        anchor=Path(doc['observation_anchor']['path']);a=json.loads(anchor.read_text());a['results'][0]['files']['initial-copy-export.json']['sha256']=digest(original)
        anchor.write_text(json.dumps(a));doc['observation_anchor']['sha256']=digest(anchor)
    envelope.write_text(json.dumps(doc));args.initial_copy_job_archive['sha256']=digest(envelope)
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


def test_current_primary_namespace_is_mandatory():
    _,args,_,_=fixtures.fixture('new');job=fixtures.job(args,'key')
    for namespace in (None,'foreign'):
        job['metadata']['namespace']=namespace
        with pytest.raises(ValueError):collect.resolve_initial_copy_job([job],args,'key',None)


@pytest.mark.parametrize('claim',['partial','true','data-false'])
def test_limited_manifest_cannot_upgrade_unknown_claims(tmp_path,claim):
    from quadringent.qualification import cli
    manifest={'kind':'quadringent-retained-history-v1','scope':{},'epochs':[{}],'known_copy_runs':['old'],'observed_utc':'2026-10-04T22:00:00Z','native_cdc_qualified':False,
              **collect.LIMITED_JOB_CLAIMS,'retained_history_data_qualified':True}
    if claim=='partial':manifest.pop('ttl_observed')
    elif claim=='true':manifest['historical_identity_verified']=True
    else:manifest['retained_history_data_qualified']=False
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest));path.chmod(0o600)
    with pytest.raises(ValueError):cli.retained_manifest({'retained_history_manifest':str(path),'retained_history_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.mark.parametrize('side',[0,1])
@pytest.mark.parametrize('location',['top','primary-evidence'])
@pytest.mark.parametrize('marker',['inventory','source'])
def test_evaluate_never_qualifies_limited_primary(tmp_path,side,location,marker):
    import test_native_qualification_cli as evaluate_fixtures
    from quadringent.qualification import cli
    data=evaluate_fixtures.fixture(tmp_path)
    snapshot=data[side]
    primary=snapshot if location=='top' else snapshot.setdefault('initial_copy_job_evidence',{})
    if marker=='inventory':primary['old_jobs_inventory_complete']=False
    else:primary['job_evidence_source']='archived_api_projection_limited'
    data[5].write_text(json.dumps(data[0]));data[3][0]['baseline_sha256']=cli.sha(data[5])
    with pytest.raises(ValueError,match="limited historical primary"):
        cli.evaluate(*data,expected_history_mode='streaming')


def test_evaluate_live_primary_can_keep_limited_historical_epochs(tmp_path):
    import test_native_qualification_cli as evaluate_fixtures
    from quadringent.qualification import cli
    data=evaluate_fixtures.fixture(tmp_path)
    for snapshot in data[:2]:
        snapshot['job_evidence_source']='live_api'
        snapshot['initial_copy_job_evidence']={'job_evidence_source':'live_api'}
        snapshot['retained_history']={'old_jobs_inventory_complete':False,'epochs':[{'job_evidence_source':'archived_api_projection_limited'}]}
    data[5].write_text(json.dumps(data[0]));data[3][0]['baseline_sha256']=cli.sha(data[5])
    assert cli.evaluate(*data,expected_history_mode='streaming')['native_cdc_qualified'] is True


def test_v2_primary_wrapper_requires_current_reader(tmp_path,monkeypatch):
    args,key,job,probe,doc,envelope,digest=archive_fixture(tmp_path)
    args.initial_copy_revision=args.expected_revision
    original=Path(doc['api_document']['path'])
    original.write_text(json.dumps({'observed_utc':'2026-10-04T20:02:00Z','job':job}));ref={'path':str(original),'sha256':digest(original)}
    doc.update(kind='quadringent-job-api-archive-v2',api_document=ref,observation_anchor=ref,api_document_pointer=['job'],api_sha256_pointer=None)
    envelope.write_text(json.dumps(doc));args.initial_copy_job_archive['sha256']=digest(envelope)
    with pytest.raises((ValueError,AttributeError)):collect.resolve_initial_copy_job([],args,key,probe)
    args.reader_deployment='reader'
    old_api=probe.initial_copy_job_api
    probe.initial_copy_job_api=lambda name:{**old_api(name),'namespace_witness':{'deployment':'reader','uid':'reader-uid'}}
    monkeypatch.setattr(collect,'primary_archive_reader',lambda p,a: {'uid':'reader-uid','pod_uid':'pod-uid'})
    result=collect.resolve_initial_copy_job([],args,key,probe)
    assert result['job_evidence_source']=='archived_api'
    assert result['archived_jobs_inventory_complete'] is False


@pytest.mark.parametrize('change',['hash','scope','uid','image','ttl','utc','pointer','multiple','fake','missing-witness','foreign-witness'])
def test_v2_primary_refuses_corruption(tmp_path,monkeypatch,change):
    args,key,job,probe,doc,envelope,digest=archive_fixture(tmp_path)
    args.initial_copy_revision=args.expected_revision;args.reader_deployment='reader'
    wrapper={'observed_utc':'2026-10-04T20:02:00Z','job':job}
    if change=='uid':doc['job_uid']='foreign'
    if change=='scope':doc['scope']['namespace']='foreign'
    if change=='image':job['spec']['template']['spec']['containers'][0]['image']='wrong@sha256:'+'e'*64
    if change=='ttl':job['spec'].pop('ttlSecondsAfterFinished')
    if change=='utc':wrapper['observed_utc']='2026-10-04T22:00:00Z'
    if change=='fake':job.pop('apiVersion')
    if change=='multiple':wrapper['job']=[job,job]
    original=Path(doc['api_document']['path']);original.write_text(json.dumps(wrapper))
    ref={'path':str(original),'sha256':digest(original)}
    doc.update(kind='quadringent-job-api-archive-v2',api_document=ref,observation_anchor=ref,api_document_pointer=['job'],api_sha256_pointer=None)
    if change=='pointer':doc['api_document_pointer']=['missing']
    envelope.write_text(json.dumps(doc));args.initial_copy_job_archive['sha256']=digest(envelope)
    if change=='hash':original.write_text(original.read_text()+' ')
    old_api=probe.initial_copy_job_api
    probe.initial_copy_job_api=lambda name:{**old_api(name),**({} if change=='missing-witness' else {'namespace_witness':{'deployment':'reader','uid':'foreign' if change=='foreign-witness' else 'reader-uid'}})}
    monkeypatch.setattr(collect,'primary_archive_reader',lambda p,a:{'uid':'reader-uid','pod_uid':'pod-uid'})
    with pytest.raises(ValueError):collect.resolve_initial_copy_job([],args,key,probe)


@pytest.mark.parametrize('change',['table','schema','journal','boundary','uid','pod','candidate'])
def test_primary_reader_bootstrap_is_real_and_bounded(monkeypatch,change):
    _,args,_,_=fixtures.fixture();args.reader_deployment='reader';args.journal_library='J';args.journal_name='JN'
    reader={'uid':'uid','pod_uid':'pod'}
    entry={'table_id':args.table_id,'schema':'S','table':'T','receiver':'R1','sequence':5}
    env={'AS400_TABLE_BOOTSTRAP_JSON':json.dumps([entry]),'AS400_JOURNAL_LIBRARY':'J','AS400_JOURNAL_NAME':'JN'}
    monkeypatch.setattr(collect.n,'workload_candidate',lambda p,c: (_ for _ in ()).throw(ValueError('candidate')) if change=='candidate' else None)
    if change=='table':entry['table_id']='foreign'
    if change=='schema':entry['schema']='FOREIGN'
    if change=='boundary':entry['sequence']=11
    if change=='journal':env['AS400_JOURNAL_NAME']='FOREIGN'
    if change=='uid':reader.pop('uid')
    if change=='pod':reader.pop('pod_uid')
    env['AS400_TABLE_BOOTSTRAP_JSON']=json.dumps([entry]);probe=SimpleNamespace(workload=lambda *a:(reader,env))
    with pytest.raises(ValueError):collect.primary_archive_reader(probe,args)


def test_primary_v1_and_manifest_inventory_keep_old_runs(tmp_path,monkeypatch):
    from quadringent.qualification import cli
    args,key,job,probe,*_=archive_fixture(tmp_path)
    args.initial_copy_revision=args.expected_revision;args.reader_deployment='reader';args.loader_deployment='loader';args.loader_digest='loader-digest'
    old=deepcopy(job);old['metadata']['annotations']['quadringent.io/run-id']='previous';old['metadata']['uid']='previous-uid'
    probe.kubectl=lambda *a:{'items':[old]}
    probe.workload=lambda *a:({}, {})
    old_api=probe.initial_copy_job_api
    probe.initial_copy_job_api=lambda name:{**old_api(name),'namespace_witness':{'deployment':'reader','uid':'reader-uid'}}
    monkeypatch.setattr(collect,'primary_archive_reader',lambda p,a:{'uid':'reader-uid','pod_uid':'pod-uid'})
    monkeypatch.setattr(collect.LiveCollector,'copy',lambda *a,**kw:{'artifacts':[{'evidence_key':key}]})
    assert cli.history_inventory_gate(probe,{'known_copy_runs':['previous']})==[old]
    extra=deepcopy(old);extra['metadata']['annotations']['quadringent.io/run-id']='unknown'
    probe.kubectl=lambda *a:{'items':[old,extra]}
    with pytest.raises(ValueError):cli.history_inventory_gate(probe,{'known_copy_runs':['previous']})


def test_primary_reader_accepts_authentic_earlier_bootstrap(monkeypatch):
    _,args,_,_=fixtures.fixture();args.reader_deployment='reader';args.journal_library='J';args.journal_name='JN'
    reader={'uid':'uid','pod_uid':'pod'}
    env={'AS400_TABLE_BOOTSTRAP_JSON':json.dumps([{'table_id':args.table_id,'schema':'S','table':'T','receiver':'R1','sequence':5}]),'AS400_JOURNAL_LIBRARY':'J','AS400_JOURNAL_NAME':'JN'}
    monkeypatch.setattr(collect.n,'workload_candidate',lambda p,c:None)
    assert collect.primary_archive_reader(SimpleNamespace(workload=lambda *a:(reader,env)),args)==reader
