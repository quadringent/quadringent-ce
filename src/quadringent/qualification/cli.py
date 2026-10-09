"""Phases natives explicites, configuration et preuves privées, erreurs expurgées."""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from . import actions, collect, proof, release_admission as admission

PHASES = ('receiver-catalog','evaluate-rotation','retain-history','admit-release','oracle','baseline','snapshot','observe','positions','pause','resume','bind-mutations','bind-rotation-mutations','evaluate','crash','bind-crash-mutations','evaluate-crash')
REFS = {'rotation_proof','catalog_before','catalog_after','crash_proof','before','after','history_oracle','baseline','receipt','ack_receipts','pause_proof','resume_proof','mutation_receipts','observations','observation_receipts'}


def sha(path):
    return hashlib.sha256(admission.read_regular(path, admission.MAX_RECEIPT_BYTES)).hexdigest()


def private_json(path):
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    if mode != 0o600:raise ValueError("private 0600 file required")
    return admission.document(admission.read_regular(path, admission.MAX_RECEIPT_BYTES))


def private_dir(path):
    path = Path(path)
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError("symlink directory refused")
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("private 0700 directory required")
    return path


def validate_actions(value):
    if not isinstance(value,dict) or set(value)-{'enabled','environment','api_config','crash_reader'}:
        raise ValueError("Unsupported actions")
    if value.get('crash_reader') is True:
        actions.validate_crash_actions(value)
        if 'api_config' not in value:return None
    if value.get('enabled',False) is False:return None
    if value.get('enabled') is not True or value.get('environment') != 'dev' or not isinstance(value.get('api_config'),str):
        raise ValueError("Native actions require explicit DEV activation")
    return value['api_config']


def release_expected(value):
    required={'mode','repository','visibility','run_id','attempt','source_sha','tag','workflow_sha256',
              'artifact_id','artifact_zip','metadata_artifact_id','metadata_artifact_zip','metadata_root'}
    if not isinstance(value,dict) or not required <= set(value) or set(value)-required-{'admission','admission_sha256'}:
        raise ValueError("incomplete admission configuration")
    if value['mode'] not in {'fresh','conserved'} or value['visibility'] not in {'private','public'}:
        raise ValueError("explicit visibility and provenance required")
    return admission.Expected(value['run_id'],value['attempt'],value['source_sha'],value['tag'],value['workflow_sha256'],
                              value['repository'],value['visibility']=='private')


def release_proof(value,output):
    expected=release_expected(value);root=Path(value['metadata_root'])
    if value['mode']=='fresh':
        report,raw=admission.admit(expected,root,value['artifact_zip'],value['artifact_id'],
                                   admission.GitHub(expected.repository),datetime.now(timezone.utc),
                                   metadata_archive=value['metadata_artifact_zip'],metadata_artifact_id=value['metadata_artifact_id'])
        # Conserve aussi les identités originales des archives pour vérification ultérieure hors rétention CI.
        report['conserved_artifacts']={}
        for key,path,maximum,digest_field in (
                ('receipt',value['artifact_zip'],admission.MAX_ZIP_BYTES,'artifact_zip_digest'),
                ('metadata',value['metadata_artifact_zip'],admission.MAX_METADATA_ZIP_BYTES,'metadata_artifact_zip_digest')):
            archive_bytes=admission.read_regular(path,maximum)
            archive_digest=admission.digest(archive_bytes)
            if archive_digest!=report['authenticated_ci'][digest_field]:
                raise ValueError("archive changed before retaining the anchor")
            report['conserved_artifacts'][key]={'digest':archive_digest,'size_in_bytes':len(archive_bytes)}
        private_dir(root)
        receipt=root/'passed.json'
        if receipt.exists():
            if admission.read_regular(receipt,admission.MAX_RECEIPT_BYTES) != raw:raise ValueError("retained receipt differs")
        else:admission.write_private(receipt,raw)
        proof.write_private(output,report)
        return output
    # Le SHA d'admission est une ancre locale explicitement épinglée par l'opérateur lors de l'admission fraîche.
    # Ce mode n'affirme aucune authentification GitHub nouvelle après expiration des artefacts.
    path=value.get('admission');pinned=value.get('admission_sha256','')
    if not path or not admission.HEX.fullmatch(pinned) or sha(path) != pinned:
        raise ValueError("retained admission not pinned")
    report=private_json(path);ci=report['authenticated_ci']
    if (ci['repository']!=expected.repository or ci['run_id']!=expected.run_id or ci['run_attempt']!=expected.attempt or
            ci['workflow_sha256']!=expected.workflow_sha256 or ci['artifact_id']!=value['artifact_id'] or
            ci['metadata_artifact_id']!=value['metadata_artifact_id'] or report['expected_private']!=expected.private or
            report['tag']!=expected.tag or report['revision']!=expected.source):
        raise ValueError("retained admission outside the release")
    archives=report['conserved_artifacts']
    if archives['receipt']['digest']!=ci['artifact_zip_digest'] or archives['metadata']['digest']!=ci['metadata_artifact_zip_digest']:
        raise ValueError("archives outside the authenticated evidence")
    raw,files=admission.receipt_bytes(value['artifact_zip'],archives['receipt'])
    originals=admission.metadata_archive_bytes(value['metadata_artifact_zip'],archives['metadata'],files)
    admission.materialize_metadata(root,originals,receipt_raw=raw)
    if admission.read_regular(root/'passed.json',admission.MAX_RECEIPT_BYTES)!=raw:raise ValueError("retained receipt altered")
    proof.release_bindings(root,root/'passed.json',expected.source,path)
    return path


def load_site(path, *, admission_only=False):
    data=private_json(path)
    if not isinstance(data,dict) or set(data)-{'native','release','initial_copy_release','refs','actions'} or 'release' not in data or (not admission_only and 'native' not in data):
        raise ValueError("unknown native configuration")
    refs=data.get('refs',{})
    if not isinstance(refs,dict) or set(refs)-REFS:raise ValueError("unknown references")
    for key,value in refs.items():
        paths=value if key in {'observations','observation_receipts'} else [value]
        if not isinstance(paths,list) or not paths or any(not isinstance(p,str) for p in paths):raise ValueError("invalid references")
        for item in paths:private_json(item)
    validate_actions(data.get('actions',{}));release_expected(data['release'])
    if 'initial_copy_release' in data:
        release_expected(data['initial_copy_release'])
        if data['initial_copy_release']['mode']!='conserved':
            raise ValueError("prior pinned initial-copy admission required")
    if admission_only:return data
    native=data['native']
    if not isinstance(native,dict) or set(native)-collect.ALLOWED or native.get('expected_history_mode') not in proof.HISTORY_MODES:
        raise ValueError("An explicit native HISTORY contract is required (streaming or sql)")
    # Les chemins et identités admission sont exclusivement dérivés du contrat release, jamais d'un booléen fourni.
    if set(native)&{'release_oci_root','release_receipt','release_admission','expected_revision'}:
        raise ValueError("duplicate release configuration refused")
    private_json(native['columns'])
    for key in ('namespace','reader_deployment','loader_deployment'):
        if not isinstance(native.get(key),str) or re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',native[key]) is None:
            raise ValueError("invalid Kubernetes scope")
    if not isinstance(native.get('context'),str) or not native['context'] or native['context'].startswith('-') or any(c.isspace() for c in native['context']):
        raise ValueError("Invalid explicit Kubernetes context")
    kubeconfig=Path(native['kubeconfig'])
    if not kubeconfig.is_absolute() or any(p.is_symlink() for p in [kubeconfig,*kubeconfig.parents]):
        raise ValueError("explicit regular kubeconfig required")
    if not stat.S_ISREG(kubeconfig.stat().st_mode):raise ValueError("regular kubeconfig required")
    if not isinstance(native.get('pipeline'),str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}',native['pipeline']) is None:
        raise ValueError("Invalid pipeline")
    return data


def evaluate(before,after,operations,observations,receipts,baseline,*,expected_history_mode):
    for snapshot in (before,after):
        for primary in (snapshot,snapshot.get('initial_copy_job_evidence',{})):
            if primary.get('old_jobs_inventory_complete') is False or primary.get('job_evidence_source')=='archived_api_projection_limited':
                raise ValueError("limited historical primary does not qualify native CDC")
        for component in ('reader','loader'):
            if snapshot.get(component,{}).get('image_binding',{}).get('verified') is not True:
                raise ValueError("executed release binding absent")
    result=proof.evaluate_resume(before,after,None,operations)
    proof.validate_history_modes(expected_history_mode, before, after)
    if len(observations)!=len(receipts) or len(receipts)!=len(operations['mutations']) or not 1<=len(receipts)<=20:
        raise ValueError("incomplete latency coverage")
    scope={k:before[k] for k in collect.SCOPE};seen=set();bounds=[]
    for observed,path,bound in zip(observations,receipts,operations['mutations'],strict=True):
        ack=private_json(path);digest=sha(path)
        if (digest in seen or observed.get('receipt_sha256')!=digest or observed.get('baseline_sha256')!=sha(baseline) or
                any(observed.get(k)!=v or ack.get(k)!=v for k,v in scope.items()) or
                observed.get('status')!='visible' or observed.get('latency_kind')!='first_observed_upper_bound' or
                observed.get('threshold_s')!=10 or observed.get('observed_threshold_met') is not True or
                observed.get('history_mode')!=expected_history_mode or
                ack.get('expected',ack.get('mutation_identity'))!=ack.get('mutation_identity') or
                any(ack.get(k)!=bound.get(k) for k in ('write_started_utc','write_ack_utc','operation','mutation_identity')) or
                any(observed.get(k)!=ack.get(k) for k in ('write_started_utc','write_ack_utc'))):
            raise ValueError("latency observation not bound to source ACKs")
        proof.validate_receipt(ack,before['observed_utc']);seen.add(digest)
        for layer in ('history','mirror'):
            durations=proof.latencies(ack['write_started_utc'],ack['write_ack_utc'],observed[layer+'_visible_utc'])
            if max(durations.values())>10:raise ValueError("visibility exceeds 10 seconds")
            bounds.append(durations['from_start_s'])
    if private_json(baseline)!=before:raise ValueError("latency baseline differs from before")
    return {**result,'native_cdc_qualified':True,'scope':'same_receiver_pause_mutations_resume_and_observed_visibility',
            'history_mode':expected_history_mode,'observed_threshold_met':True,'observed_mutations':len(receipts),'observed_max_from_start_s':max(bounds),
            'maximum_10s_qualified':False,'rotation_qualified':False,
            'limitations':['receiver_rotation_not_observed','observed_bounds_are_not_a_universal_latency_slo']}


class DeadlineApi:
    """Adaptateur local du client installé : timeout résiduel avant chaque appel."""
    def __init__(self,client,deadline):self.client=client;self.deadline=deadline
    def __enter__(self):self.client.__enter__();return self
    def __exit__(self,*args):self.client.__exit__(*args)
    def call(self,method,*args,**kwargs):
        remaining=min(30,self.deadline-time.monotonic())
        if remaining<=0:raise ValueError("API deadline expired")
        if hasattr(self.client,'_http'):
            import httpx
            self.client._http.timeout=httpx.Timeout(remaining)
        return getattr(self.client,method)(*args,**kwargs)
    def get(self,*args,**kwargs):return self.call('get',*args,**kwargs)
    def write(self,*args,**kwargs):return self.call('write',*args,**kwargs)


def native_action(phase,args,actions,*,on_request=None):
    if phase not in {'pause','resume'}:raise ValueError("action outside the scope")
    path=validate_actions(actions)
    if path is None:return {'executed':False,'mode':'read_only_observation'}
    private_json(path)
    from quadringent.installer.api_client import ApiClient,load_config
    # env={} empêche la configuration explicite de viser une autre cible via l'environnement local.
    config=load_config(env={},config_path=Path(path))
    if not config.token:raise ValueError("authenticated API action required")
    start=proof.utc()
    deadline=getattr(args,'deadline',time.monotonic()+60)
    with DeadlineApi(ApiClient(config),deadline) as client:
        selected=client.get(f'/v2/pipelines/{args.pipeline}')
        if selected.exit_code!=0 or not isinstance(selected.body,dict) or selected.body.get('id')!=args.pipeline:
            raise ValueError("API pipeline is missing")
        cursor=None;seen=set();found: list[dict]=[]
        for _ in range(20):
            if time.monotonic()>=deadline:raise ValueError("API listing exceeds the budget")
            listed=client.get('/v2/pipelines',params={'limit':100,**({'cursor':cursor} if cursor else {})})
            if listed.exit_code!=0 or not isinstance(listed.body,dict) or not isinstance(listed.body.get('items'),list):
                raise ValueError("API scope not observable")
            found.extend(item for item in listed.body['items'] if item.get('id')==args.pipeline)
            cursor=listed.body.get('next_cursor')
            if cursor is None:break
            if not isinstance(cursor,str) or cursor in seen:raise ValueError("inconsistent API pagination")
            seen.add(cursor)
        else:raise ValueError("API listing is too large")
        if len(found)!=1 or found[0].get('table_id')!=args.table_id:
            raise ValueError("API pipeline does not match the table")
        key='native-qualification-'+uuid.uuid4().hex
        request={'action':phase,'pipeline':args.pipeline,'table_id':args.table_id,'environment':'dev',
                 'started_utc':start,'idempotency_key':key,'executed':None,'status':'request_planned'}
        if on_request:on_request(request)
        result=client.write('POST',f'/v2/pipelines/{args.pipeline}/actions/{phase}',json_body={'dry_run':False},idempotency_key=key)
        if result.exit_code!=0:raise ValueError("native action refused or acknowledgment absent")
        verified=client.get(f'/v2/pipelines/{args.pipeline}')
        expected_state='paused' if phase=='pause' else 'live'
        if verified.exit_code!=0 or not isinstance(verified.body,dict) or verified.body.get('id')!=args.pipeline or verified.body.get('declared_state')!=expected_state:
            raise ValueError("native API transition not verified")
    return {'executed':True,'environment':'dev','action':phase,'pipeline':args.pipeline,
            'started_utc':start,'acknowledged_utc':proof.utc(),'idempotency_key':key}


def native_arguments(data, admission_path):
    release = data['release']
    args=collect.config_values({**data['native'], 'release_oci_root': release['metadata_root'],
                                 'release_receipt': str(Path(release['metadata_root']) / 'passed.json'),
                                 'release_admission': str(admission_path), 'expected_revision': release['source_sha']})
    if 'initial_copy_release' in data:
        initial=data['initial_copy_release']
        initial_admission=release_proof(initial,Path(admission_path).with_name('initial-copy.admission.json'))
        args.initial_copy_oci_root=initial['metadata_root']
        args.initial_copy_receipt=str(Path(initial['metadata_root'])/'passed.json')
        args.initial_copy_revision=initial['source_sha']
        args.initial_copy_admission=str(initial_admission)
        images=proof.release_bindings(args.initial_copy_oci_root,args.initial_copy_receipt,
                                      args.initial_copy_revision,args.initial_copy_admission)
        args.initial_copy_capture_digest=images['capture']['index']
    return args


def pinned_document(ref):
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or
            not isinstance(ref['sha256'], str) or not admission.HEX.fullmatch(ref['sha256']) or
            sha(ref['path']) != ref['sha256']):
        raise ValueError("retained evidence unpinned or changed")
    return private_json(ref['path'])


def retained_manifest(native):
    path = native.get('retained_history_manifest')
    pin = native.get('retained_history_sha256')
    if path is None and pin is None:
        return None
    manifest = pinned_document({'path': path, 'sha256': pin})
    if (not isinstance(manifest, dict) or set(manifest)-set(collect.LIMITED_JOB_CLAIMS)-{'retained_history_data_qualified'} != {'kind', 'scope', 'epochs', 'known_copy_runs', 'observed_utc', 'native_cdc_qualified'} or
            manifest['kind'] != 'quadringent-retained-history-v1' or manifest['native_cdc_qualified'] is not False or
            not isinstance(manifest['epochs'], list) or not 1 <= len(manifest['epochs']) <= collect.MAX_RETAINED_EPOCHS):
        raise ValueError("invalid epoch manifest")
    limitations=set(collect.LIMITED_JOB_CLAIMS)|{'retained_history_data_qualified'}
    if set(manifest)&limitations and (not limitations<=set(manifest) or any(manifest[k] is not False for k in collect.LIMITED_JOB_CLAIMS) or manifest['retained_history_data_qualified'] is not True):
        raise ValueError("invalid limited historical claims")
    return manifest


def validate_retained_snapshot(snapshot, oracle, old_probe):
    args = old_probe.args
    collect.scope_matches(snapshot, args)
    if (snapshot.get('snapshot_checks_passed') is not True or snapshot.get('copy_job_observed_succeeded') is not True or
            snapshot.get('duplicate_event_ids') != 0 or snapshot.get('history_mode') != getattr(args, 'expected_history_mode', 'streaming') or
            snapshot.get('checkpoint') != oracle['checkpoint'] or
            snapshot.get('reconciliation', {}).get('equal') is not True):
        raise ValueError("retained baseline not qualified")
    # L'image observée ancienne est recalculée contre sa propre admission, jamais celle du runtime courant.
    for key, component, expected in (('reader', 'capture', args.capture_digest), ('loader', 'control-plane', args.loader_digest)):
        workload = snapshot[key]
        binding = workload['image_binding']
        images = [i for i in workload['images'] if i.get('name') == workload['container']]
        if len(images) != 1 or proof.image_binding(old_probe.release_images[component], expected,
                images[0]['image_id'], binding['architecture']) != binding:
            raise ValueError("Historical image does not match the prior admission")
        proof.workload_candidate(workload, workload['container'])
        expected_identity={name:getattr(args,name,None) for name in (
            'expected_gcp_service_account','expected_gcp_project','expected_gcs_bucket',
            'expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type')}
        proof.identity_proof(workload['identity_effective'],args.identity,args.expected_role_arn,
                             expected_gcs_key=getattr(args,'gcs_read_key',None),**expected_identity)
    proof.history_proof(proof.normalize_history(snapshot['history'], old_probe.columns), oracle['events'])
    if not proof.compare_rows(snapshot['source'], snapshot['mirror'], old_probe.columns, args.pk)['equal']:
        raise ValueError('source/miroir historique divergent')


def revalidate_retained(manifest, probe, collector, reader, loader, jobs):
    if manifest is None:
        return []
    if manifest['scope'] != {k: getattr(probe.args, k) for k in collect.SCOPE}:
        raise ValueError("Manifest does not match the site/table")
    oracles: list[dict] = []
    runs = []
    archived_runs = []
    for entry in manifest['epochs']:
        if not isinstance(entry, dict) or set(entry) != {'config', 'bundle'}:
            raise ValueError("unknown epoch reference")
        old_data = pinned_document(entry['config'])
        # load_site valide les vrais chemins privés et le contrat natif, pas un document arbitraire.
        if load_site(entry['config']['path']) != old_data or old_data['release']['mode'] != 'conserved':
            raise ValueError("invalid epoch configuration")
        old_admission = release_proof(old_data['release'], Path(entry['bundle']['path']).with_suffix('.admission.json'))
        old_args = native_arguments(old_data, old_admission)
        collect.same_retained_scope(old_args, probe.args)
        old_probe = proof.NativeProbe(old_args)
        if old_probe.columns != probe.columns:
            raise ValueError("historical columns differ")
        bundle = pinned_document(entry['bundle'])
        if set(bundle) != {'kind', 'parts', 'job', 'oracle', 'snapshot', 'observed_utc'} or bundle['kind'] != 'quadringent-retained-epoch-v1':
            raise ValueError("invalid epoch bundle")
        parts = bundle['parts']
        old_oracle = collect.validate_retained_parts(parts, old_args, probe.columns)
        for key in ('kind', *collect.SCOPE, 'copy_run_id', 'copy_boundary', 'events', 'checkpoint', 'copy_artifacts', 'source_journal_positions', 'journal_scan'):
            if old_oracle.get(key) != bundle['oracle'].get(key):
                raise ValueError("historical oracle changed")
        key = bundle['job']['evidence_key']
        live_copy = collector.copy(loader, args=old_args, evidence_key=key)
        if live_copy != parts['copy']:
            raise ValueError("retained snapshot bytes changed")
        job = collect.resolve_initial_copy_job(jobs, old_args, key, probe, sealed=True)
        if job.get('job_evidence_source') in collect.ARCHIVED_JOB_SOURCES:archived_runs.append(old_args.copy_run_id)
        if job.get('old_jobs_inventory_complete') is False and (any(manifest.get(k) is not False or bundle['job'].get(k) is not False for k in collect.LIMITED_JOB_CLAIMS) or manifest.get('retained_history_data_qualified') is not True):
            raise ValueError("Historical limits are omitted from the manifest or bundle")
        if collect.job_binding(job) != collect.job_binding(bundle['job']):
            raise ValueError("retained Job changed")
        if proof.stamp(job['created_utc']) > proof.stamp(bundle['observed_utc']):
            raise ValueError("Job created after the retained capture")
        if proof.stamp(bundle['observed_utc']) > proof.stamp(manifest['observed_utc']):
            raise ValueError("epoch captured after the anchor")
        # Chaque baseline scellée couvre exactement toutes les époques connues à sa capture.
        accumulated = collect.union_history_oracles(old_oracle, oracles) if oracles else old_oracle
        validate_retained_snapshot(bundle['snapshot'], accumulated, old_probe)
        oracles.append(old_oracle)
        runs.append(old_args.copy_run_id)
    if len(set(runs)) != len(runs) or sorted(runs) != manifest['known_copy_runs']:
        raise ValueError("epoch omitted or duplicated in the manifest")
    # Toute ancienne copie réussie encore présente doit avoir été conservée ; aucun fallback mono-époque.
    expected = sorted(set(runs) | {probe.args.copy_run_id})
    if probe.args.copy_run_id not in collect.successful_copy_runs(jobs,probe.args) and getattr(probe.args,'initial_copy_job_archive',None):
        copied=collector.copy(loader)
        primary=collect.resolve_initial_copy_job(jobs,probe.args,copied['artifacts'][0]['evidence_key'],probe)
        if primary.get('job_evidence_source')=='archived_api_projection_limited':raise ValueError("primary limited to data")
        archived_runs.append(probe.args.copy_run_id)
    collect.inventory_matches(jobs,probe.args,expected,archived_runs)
    return oracles


HISTORY_PHASES = frozenset({'snapshot', 'observe', 'evaluate', 'evaluate-crash','evaluate-rotation'})


def history_inventory_gate(probe, manifest):
    """Aucun snapshot/oracle fourni ne peut contourner l'inventaire réel des Jobs."""
    jobs=probe.kubectl('get','jobs','-l',f'quadringent.io/pipeline-id={probe.args.pipeline}','-o','json')['items']
    actual=collect.successful_copy_runs(jobs,probe.args)
    if manifest is None:
        expected=[probe.args.copy_run_id]
    else:
        retained=manifest['known_copy_runs']
        if (not isinstance(retained,list) or not retained or len(retained)!=len(set(retained)) or
                probe.args.copy_run_id in retained):
            raise ValueError("invalid retained inventory")
        expected=sorted([*retained,probe.args.copy_run_id])
    archived=[]
    if manifest is None and actual!=expected and getattr(probe.args,'initial_copy_job_archive',None):
        loader=probe.workload(probe.args.loader_deployment,probe.args.loader_digest)
        copied=collect.LiveCollector(probe).copy(loader)
        key=copied['artifacts'][0]['evidence_key']
        job=collect.resolve_initial_copy_job(jobs,probe.args,key,probe)
        if job.get('job_evidence_source') in collect.ARCHIVED_JOB_SOURCES:archived=[probe.args.copy_run_id]
    elif manifest is not None:
        # Anciennes absences revalidées avant dispatch ; primaire absent exige son archive stricte.
        archived=[*manifest['known_copy_runs']]
        if probe.args.copy_run_id not in actual and getattr(probe.args,'initial_copy_job_archive',None):
            loader=probe.workload(probe.args.loader_deployment,probe.args.loader_digest)
            copied=collect.LiveCollector(probe).copy(loader)
            collect.resolve_initial_copy_job(jobs,probe.args,copied['artifacts'][0]['evidence_key'],probe)
            archived.append(probe.args.copy_run_id)
    collect.inventory_matches(jobs,probe.args,expected,archived)
    return jobs


def retained_oracle(collector, manifest, *, capture_parts=False):
    probe = collector.probe
    current = collector.oracle(capture_parts=capture_parts)
    jobs = probe.kubectl('get', 'jobs', '-l', f'quadringent.io/pipeline-id={probe.args.pipeline}', '-o', 'json')['items']
    reader = (current['reader'], None)
    loader = (current['loader'], None)
    retained = revalidate_retained(manifest, probe, collector, reader, loader, jobs)
    if manifest is not None:
        current_key=current['copy_artifacts'][0]['evidence_key']
        current_job=collect.resolve_initial_copy_job(jobs,probe.args,current_key,probe)
        if proof.stamp(manifest['observed_utc']) >= proof.stamp(current_job['created_utc']):
            raise ValueError("retained anchor must precede the new copy")
    key=current['copy_artifacts'][0]['evidence_key']
    primary=collect.resolve_initial_copy_job(jobs,probe.args,key,probe)
    if manifest is not None and primary.get('job_evidence_source')=='archived_api_projection_limited':raise ValueError("primary limited to data")
    if manifest is None:
        archived=[probe.args.copy_run_id] if primary.get('job_evidence_source') in collect.ARCHIVED_JOB_SOURCES else []
        collect.inventory_matches(jobs,probe.args,[probe.args.copy_run_id],archived)
    return (collect.union_history_oracles(current, retained) if retained else current), current, jobs


def run_native(config,out_dir,phase,*,stdout=None):
    stdout=stdout or sys.stdout
    try:
        if phase not in PHASES:raise ValueError("unknown phase")
        data=load_site(config,admission_only=phase=='admit-release');out=private_dir(out_dir);target=out/(phase+'.json')
        if target.exists() or (out/(phase+'.action.json')).exists() or (out/(phase+'.action-request.json')).exists():raise ValueError("Evidence or action already exists")
        release=data['release']
        if phase=='admit-release' and release['mode']!='fresh':raise ValueError("fresh admission required")
        if phase!='admit-release' and release['mode']!='conserved':raise ValueError("prior pinned admission required")
        admission_path=release_proof(release,target if phase=='admit-release' else out/(phase+'.admission.json'))
        if phase=='admit-release':
            print(json.dumps({'status':'admitted','admission_sha256':sha(admission_path),'proof':str(target.resolve()),
                              'native_cdc_qualified':False}),file=stdout);return 0
        native={**data['native'],'release_oci_root':release['metadata_root'],'release_receipt':str(Path(release['metadata_root'])/'passed.json'),
                'release_admission':str(admission_path),'expected_revision':release['source_sha']}
        # Réutilise le validateur natif éprouvé sans créer de configuration temporaire persistante.
        args=native_arguments(data,admission_path)
        refs=data.get('refs',{})
        for key in ('before','after','history_oracle','baseline','receipt'):setattr(args,key,refs.get(key))
        args.out=str(target);probe=proof.NativeProbe(args);probe.deadline=time.monotonic()+args.max_seconds;args.deadline=probe.deadline
        def read(key):return private_json(refs[key])
        collector=collect.LiveCollector(probe);manifest=retained_manifest(native)
        history_workloads={}
        if phase in HISTORY_PHASES:
            history_jobs=history_inventory_gate(probe,manifest)
            if manifest is not None and phase!='snapshot':
                retained_reader=probe.workload(args.reader_deployment,args.capture_digest)
                retained_loader=probe.workload(args.loader_deployment,args.loader_digest)
                revalidate_retained(manifest,probe,collector,retained_reader,retained_loader,history_jobs)
                history_workloads={args.reader_deployment:retained_reader[0],args.loader_deployment:retained_loader[0]}
        if phase=='receiver-catalog':
            reader=probe.workload(args.reader_deployment,args.capture_digest)
            loader=probe.workload(args.loader_deployment,args.loader_digest)
            report=collector.receiver_catalog(reader,collector.copy(loader))
        elif phase=='oracle':report,_,_=retained_oracle(collector,manifest)
        elif phase=='retain-history':
            report,current,jobs=retained_oracle(collector,manifest,capture_parts=True)
            oracle_path=out/'retain-history.oracle.json';proof.write_private(oracle_path,report)
            args.history_oracle=str(oracle_path);snapshot=probe.snapshot()
            validate_retained_snapshot(snapshot,report,probe)
            key=current['retained_parts']['copy']['artifacts'][0]['evidence_key']
            job,archive=collect.freeze_initial_copy_job(probe,jobs,args,key,out)
            data['native']['initial_copy_job_archive']=archive
            config_path=out/'retain-history.config.json';proof.write_private(config_path,data)
            bundle_path=out/'retain-history.epoch.json'
            proof.write_private(bundle_path,{'kind':'quadringent-retained-epoch-v1','parts':current.pop('retained_parts'),
                                             'job':job,'oracle':current,'snapshot':snapshot,'observed_utc':proof.utc()})
            epochs=[*(manifest['epochs'] if manifest else []),
                    {'config':{'path':str(config_path.resolve()),'sha256':sha(config_path)},
                     'bundle':{'path':str(bundle_path.resolve()),'sha256':sha(bundle_path)}}]
            if len(epochs)>collect.MAX_RETAINED_EPOCHS:raise ValueError("epoch budget exceeded")
            report={'kind':'quadringent-retained-history-v1','scope':{k:getattr(args,k) for k in collect.SCOPE},
                    'epochs':epochs,'known_copy_runs':sorted([*(manifest['known_copy_runs'] if manifest else []),args.copy_run_id]),
                    'observed_utc':proof.utc(),'native_cdc_qualified':False}
            if job.get('old_jobs_inventory_complete') is False or (manifest and manifest.get('old_jobs_inventory_complete') is False):
                report.update(collect.LIMITED_JOB_CLAIMS,retained_history_data_qualified=True)
        elif phase=='baseline':
            oracle,_,_=retained_oracle(collector,manifest);oracle_path=out/'baseline.oracle.json';proof.write_private(oracle_path,oracle)
            args.history_oracle=str(oracle_path);report=probe.snapshot()
        elif phase=='snapshot':
            if manifest is not None:
                oracle,_,_=retained_oracle(collector,manifest)
                oracle_path=out/'snapshot.oracle.json';proof.write_private(oracle_path,oracle)
                args.history_oracle=str(oracle_path)
            if not args.history_oracle:raise ValueError("Oracle is required")
            report=probe.snapshot()
        elif phase=='observe':
            if not args.baseline or not args.receipt:raise ValueError("Baseline and ACK are required")
            report=probe.observe()
        elif phase=='positions':
            before=read('before');collect.scope_matches(before,args)
            reader=probe.workload(args.reader_deployment,args.capture_digest)
            report={**probe.source_positions(reader,before),**{k:getattr(args,k) for k in collect.SCOPE},'observed_utc':proof.utc()}
        elif phase=='crash':
            before=read('before');collect.scope_matches(before,args)
            request,injection=actions.inject_reader_crash(probe,before,data.get('actions',{}),
                on_request=lambda event:proof.write_private(out/'crash.action-request.json',event))
            proof.write_private(out/'crash.action.json',{'request':request,'injection':injection})
            while True:
                try:report=probe.crash_state(before,request,injection);break
                except (ValueError,RuntimeError):
                    if time.monotonic()+args.poll_seconds>=probe.deadline:raise
                    time.sleep(args.poll_seconds)
        elif phase=='bind-crash-mutations':
            report=collect.bind_crash_mutation_receipts(read('ack_receipts'),read('history_oracle'),read('crash_proof'),read('before'),args)
        elif phase in {'pause','resume'}:
            before=read('before');collect.scope_matches(before,args)
            # Valide le candidat initial AVANT toute action ; reprise autorisée uniquement depuis une pause réellement observée.
            if before.get('snapshot_checks_passed') is not True:raise ValueError("baseline not qualified")
            if phase=='resume':
                paused=read('pause_proof');collect.scope_matches(paused,args)
                if paused.get('reader',{}).get('deployment_absent') is not True or paused['reader'].get('owned_pods')!=[]:
                    raise ValueError("native absence not attested")
            if phase=='pause' and validate_actions(data.get('actions',{})):
                current=probe.workload(args.reader_deployment,args.capture_digest)[0]
                if proof.workload_candidate(current,'reader')!=proof.workload_candidate(before['reader'],'reader'):
                    raise ValueError("current reader outside the baseline")
            action=native_action(phase,args,data.get('actions',{}),on_request=lambda event:proof.write_private(out/(phase+'.action-request.json'),event))
            proof.write_private(out/(phase+'.action.json'),action)
            while True:
                try:report=probe.reader_state(phase,before);break
                except (ValueError,RuntimeError):
                    if time.monotonic()+args.poll_seconds>=probe.deadline:raise
                    time.sleep(args.poll_seconds)
            report['native_action']=action
        elif phase=='bind-rotation-mutations':
            report=collect.bind_rotation_mutation_receipts(read('ack_receipts'),read('history_oracle'),read('rotation_proof'),
                read('before'),read('after'),read('catalog_before'),read('catalog_after'),args)
        elif phase=='bind-mutations':
            report=collect.bind_mutation_receipts(read('ack_receipts'),read('history_oracle'),read('pause_proof'),read('resume_proof'),read('before'),args)
        else:
            before,after=read('before'),read('after')
            for item in (before,after):collect.scope_matches(item,args)
            proof.validate_history_modes(args.expected_history_mode, before, after)
            # L'identité et le candidat exécutés sont réobservés ; aucune ressource supplémentaire n'est créée.
            for deployment,digest,key in ((args.reader_deployment,args.capture_digest,'reader'),(args.loader_deployment,args.loader_digest,'loader')):
                current=history_workloads.get(deployment)
                if current is None:current=probe.workload(deployment,digest)[0]
                if phase=='evaluate-crash' and key=='reader' and actions.reader_incarnation(current)!=actions.reader_incarnation(after[key]):
                    raise ValueError("reader incarnation or uniqueness changed after crash")
                if proof.workload_candidate(current,current['container'])!=proof.workload_candidate(after[key],current['container']):
                    raise ValueError("current candidate differs from the snapshot")
            if phase=='evaluate-rotation':
                report=proof.evaluate_rotation(before,after,{'rotation':read('rotation_proof'),'catalog_before':read('catalog_before'),
                    'catalog_after':read('catalog_after'),'mutations':read('mutation_receipts'),'columns':probe.columns,'pk':args.pk,
                    'journal_library':args.journal_library,'journal_name':args.journal_name})
            elif phase=='evaluate-crash':
                report=proof.evaluate_crash(before,after,{'crash':read('crash_proof'),'mutations':read('mutation_receipts')})
            else:
                report=evaluate(before,after,{key:read(ref) for key,ref in (('pause','pause_proof'),('resume','resume_proof'),('mutations','mutation_receipts'))},
                                [private_json(path) for path in refs['observations']],[Path(path) for path in refs['observation_receipts']],
                                refs['baseline'],expected_history_mode=args.expected_history_mode)
        if phase in {'evaluate', 'evaluate-crash', 'evaluate-rotation'}:
            report['history_mode'] = args.expected_history_mode
        proof.write_private(target,report)
        failed=isinstance(report,dict) and (report.get('snapshot_checks_passed') is False or report.get('status') in {'timeout','INCOMPLETE'} or report.get('observed_threshold_met') is False)
        print(json.dumps({'status':'INCOMPLETE' if failed else 'recorded','phase':phase,'proof':str(target.resolve()),
                          'native_cdc_qualified':isinstance(report,dict) and report.get('native_cdc_qualified') is True}),file=stdout)
        return int(failed)
    except Exception:  # Le CLI ne publie jamais une traceback contenant la configuration ou le script distant.
        print(json.dumps({'status':'INCOMPLETE','phase':phase,'native_cdc_qualified':False,
                          'reason':'private_configuration_or_native_evidence_unavailable'}),file=stdout)
        return 1


def add_parser(subparsers):
    group=subparsers.add_parser('qualification',help="Qualify an existing native installation")
    commands=group.add_subparsers(dest='qualification_mode',required=True)
    native=commands.add_parser('native',help="Private native evidence, read-only by default")
    native.add_argument('--config',required=True)
    native.add_argument('--out-dir',required=True)
    native.add_argument('--phase',choices=PHASES,default='baseline',
                        help="retain-history retains an epoch before upgrade, bound to its actual release; "
                             "native.retained_history_manifest + retained_history_sha256 then pin "
                             "the independent epochs for the oracle/baseline. Optional initial_copy_release "
                             "separately admits the historical Job without changing the reader/loader release "
                             "(no HISTORY deletion)")
