"""Collecte native bornée en lecture seule, depuis les conteneurs natifs.

Oracle HISTORY indépendant de Snowflake : instantané publié lié au Job,
plus relecture DISPLAY_JOURNAL complète jusqu'au checkpoint du lecteur.
Aucune commande DML, scale, création de ressource ou lecture API Secret.
"""
from __future__ import annotations
import argparse
import base64
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from . import proof as n

SCOPE=('pipeline','table','table_id','namespace','context')
ALLOWED=set(SCOPE)|{'kubeconfig','reader_deployment','loader_deployment','capture_digest','loader_digest','columns','pk',
 'database','schema','history','mirror','journal_library','journal_name','expected_role_arn','identity','copy_run_id','copy_boundary',
 'expected_gcp_service_account','expected_gcp_project','expected_gcs_bucket','gcs_read_key','max_seconds','poll_seconds',
 'expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type',
 'release_oci_root','release_receipt','expected_revision','release_admission','expected_history_mode',
 'retained_history_manifest','retained_history_sha256','initial_copy_job_archive'}


def load_config(path):
    return config_values(json.loads(Path(path).read_text()))


def config_values(data):
    data=dict(data)
    if not isinstance(data,dict) or set(data)-ALLOWED:
        raise ValueError("unknown configuration or sensitive entries present")
    required={'kubeconfig',*SCOPE,'reader_deployment','loader_deployment','capture_digest','loader_digest','columns','pk',
              'database','schema','history','mirror','journal_library','journal_name','identity','copy_run_id','copy_boundary',
              'release_oci_root','release_receipt','expected_revision','release_admission'}
    mode=data.get('identity')
    if mode not in {'irsa','vm-metadata','gke-workload-identity','gcp-vm-metadata'}:raise ValueError("unknown identity mode")
    required|=({'expected_gcp_service_account','expected_gcp_project','expected_gcs_bucket','gcs_read_key'}
               if mode in {'gke-workload-identity','gcp-vm-metadata'} else {'expected_role_arn'})
    vm_fields={'expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type'}
    if mode=='gcp-vm-metadata':required|=vm_fields
    elif vm_fields & set(data):raise ValueError("GCP VM instance fields require the explicit mode")
    if any(data.get(k) in (None,'') for k in required):raise ValueError("incomplete native configuration")
    if mode in {'gke-workload-identity','gcp-vm-metadata'}:
        project=data['expected_gcp_project'];gsa=data['expected_gcp_service_account']
        if (not isinstance(project,str) or re.fullmatch(r'[a-z][a-z0-9-]{4,62}',project) is None or
                not isinstance(gsa,str) or re.fullmatch(r'[a-z][a-z0-9-]{0,29}@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',gsa) is None):
            raise ValueError("Invalid expected GCP project/GSA")
    if mode=='gcp-vm-metadata':
        patterns={'expected_gcp_instance_id':r'[1-9][0-9]{0,20}',
                  'expected_gcp_instance_name':r'[a-z][a-z0-9-]{0,62}',
                  'expected_gcp_zone':r'[a-z][a-z0-9-]{1,61}-[a-z]',
                  'expected_gcp_machine_type':r'[a-z][a-z0-9-]{1,62}'}
        if any(not isinstance(data[k],str) or re.fullmatch(pattern,data[k]) is None for k,pattern in patterns.items()):
            raise ValueError("Invalid expected GCP VM ID, name, zone or SKU")
    if any(n.DIGEST.fullmatch(data[k]) is None for k in ('capture_digest','loader_digest')):raise ValueError("Digests are required")
    if data.get('expected_history_mode', 'streaming') not in n.HISTORY_MODES:raise ValueError("invalid expected HISTORY mode")
    data.setdefault('expected_history_mode','streaming')
    data.setdefault('expected_role_arn',None)
    data.setdefault('max_seconds',120);data.setdefault('poll_seconds',1)
    if type(data['max_seconds']) is not int or not 1<=data['max_seconds']<=120:raise ValueError("Invalid deadline")
    if type(data['poll_seconds']) not in (int,float) or not 1 <= data['poll_seconds'] <= 10:raise ValueError("Invalid poll interval")
    for key in ('before','after','history_oracle','baseline','receipt'):
        data[key]=None
    return Namespace(**data)


def scope_matches(proof,args):
    if not isinstance(proof,dict) or any(proof.get(k)!=getattr(args,k) for k in SCOPE):
        raise ValueError("evidence outside the scope")


def history_row(record,columns,library,table):
    if record.get('source_system')!='ibmi' or record.get('library')!=library or record.get('table')!=table:
        raise ValueError("source event outside the table")
    operation=record.get('operation')
    if operation not in {'c','u_before','u_after','d'}:raise ValueError("operation cannot be qualified")
    sequence=int(n.normalize(record.get('journal_sequence'),'int'))
    if sequence<0:raise ValueError("invalid sequence")
    receiver=record.get('journal_receiver');journal=record.get('journal')
    if not isinstance(receiver,str) or not receiver or not isinstance(journal,str) or not journal:raise ValueError("Position is missing")
    event=hashlib.sha256(f'ibmi|{journal}|{receiver}|{sequence}'.encode()).hexdigest()
    if record.get('event_id')!=event:raise ValueError("event identity mismatch")
    before,after=record.get('before'),record.get('after')
    if operation in {'c','u_after'}:
        if before is not None:raise ValueError("unexpected before image")
        image=after
    else:
        if after is not None:raise ValueError("unexpected after image")
        image=before
    if not isinstance(image,dict) or not set(columns)<=set(image) or set(image)-set(columns)-{'_rrn'}:
        raise ValueError("complete image absent: RRN-only delete cannot be qualified")
    return {**{k:n.normalize(image[k],kind) for k,kind in columns.items()},
            'EVENT_ID':event,'OPERATION':operation,'JOURNAL_RECEIVER':receiver,'JOURNAL_SEQUENCE':str(sequence)}


def receiver_windows(boundary,checkpoint,catalog,args):
    """Conserve l’ordre SQL source ; aucune chaîne déduite des noms ou de l’horloge."""
    from quadringent.continuous import require_readable_receiver, require_receiver_link
    from quadringent.java_catalog import parse_catalog_output
    if (not isinstance(catalog,dict) or catalog.get('kind')!='ibmi-source-receiver-catalog' or
        catalog.get('journal_library')!=args.journal_library or catalog.get('journal_name')!=args.journal_name or
        any(catalog.get(k)!=getattr(args,k) for k in SCOPE)):
        raise ValueError("source chain catalog absent or foreign")
    n.stamp(catalog['observed_utc'])
    output=catalog.get('output')
    if not isinstance(output,str) or len(output.encode())>8192:raise ValueError("Raw catalog is missing or exceeds the budget")
    reader=catalog.get('reader',{});binding=reader.get('image_binding',{})
    if (catalog.get('output_sha256')!=hashlib.sha256(output.encode()).hexdigest() or
        not isinstance(catalog.get('jar_sha256'),str) or re.fullmatch(r'[0-9a-f]{64}',catalog['jar_sha256']) is None or
        not reader.get('uid') or not reader.get('pod_uid') or binding.get('verified') is not True or
        binding.get('index')!=args.capture_digest or not isinstance(binding.get('observed_digest'),str) or
        re.fullmatch(r'sha256:[0-9a-f]{64}',binding['observed_digest']) is None):
        raise ValueError("catalog not bound to the admitted bytes/JAR and runtime")
    rows=parse_catalog_output(output)
    if not 1<=len(rows)<=8:raise ValueError("receiver chain over budget or truncated")
    if len(rows)>1 and not rows[0].chain_links_observed:
        raise ValueError("native receiver links required for multi-receiver qualification")
    if type(boundary.get('last_sequence'))is not int or boundary['last_sequence']<0:raise ValueError("initial boundary absent")
    names=set();previous=None;windows=[];budget=0
    for index,row in enumerate(rows):
        require_readable_receiver(row)
        name=row.receiver;library=row.receiver_library;first=row.first_sequence;last=row.last_sequence
        if (re.fullmatch(r'[A-Z0-9_$#@]{1,10}',name) is None or name in names or
            re.fullmatch(r'[A-Z0-9_$#@]{1,10}',library) is None or
            type(first)is not int or type(last)is not int or not 1<=first<=last):raise ValueError("incomplete or duplicate receiver")
        if previous is not None and first!=previous+1:raise ValueError("receiver chain gap or reset")
        if index and row.chain_links_observed:
            require_receiver_link(rows[index-1],row)
        names.add(name);previous=last
        if index==0 and (name!=boundary.get('receiver_name') or library!=boundary.get('receiver_library')):
            raise ValueError("initial receiver not anchored in the copy")
        if index==len(rows)-1:
            if name!=checkpoint.get('receiver') or row.status!='ATTACHED':raise ValueError("checkpoint outside the last attached receiver")
        elif row.status=='ATTACHED':raise ValueError("receiver attached before the end of the chain")
        start=boundary['last_sequence']+1 if index==0 else first
        end=checkpoint.get('sequence') if index==len(rows)-1 else last
        if type(end)is not int or not first<=start<=last+1 or not first-1<=end<=last or end<start-1:
            raise ValueError("inconsistent receiver boundary")
        budget+=max(0,end-start+1)
        if budget>1000:raise ValueError("aggregate journal budget exceeded")
        windows.append({'receiver':name,'receiver_library':library,'start':start,'end':end})
    return windows


def validate_oracle_parts(copy,journal,positions,checkpoint,args,columns):
    boundary=copy.get('boundary',{})
    if (copy.get('pipeline_id')!=args.pipeline or copy.get('table_id')!=args.table_id or copy.get('run_id')!=args.copy_run_id or
        str(boundary.get('receiver_name'))+':'+str(boundary.get('last_sequence'))!=args.copy_boundary):
        raise ValueError("copy evidence outside the attempt/boundary")
    low=boundary.get('last_sequence');receiver=boundary.get('receiver_name')
    high=checkpoint.get('sequence') if isinstance(checkpoint,dict) else None
    multi=journal.get('kind')=='ibmi-multi-receiver-journal'
    if multi:
        windows=receiver_windows(boundary,checkpoint,journal.get('receiver_catalog'),args)
    else:
        if (type(low) is not int or type(high) is not int or low<0 or high<low or high-low>1000 or
                checkpoint.get('receiver')!=receiver):raise ValueError("checkpoint absent, rotation or window too large")
        windows=[{'receiver':receiver,'receiver_library':boundary.get('receiver_library'),'start':low+1,'end':high}]
    snapshot=copy.get('records');rows=copy.get('rows_copied')
    if type(rows) is not int or not 1<=rows<=n.MAX_ROWS or not isinstance(snapshot,list) or len(snapshot)!=rows:
        raise ValueError("complete snapshot absent")
    snapshot=sorted(snapshot,key=lambda e:int(n.normalize(e.get('journal_sequence'),'int')))
    if not copy.get('artifacts') or any(a.get('verified') is not True for a in copy['artifacts']):
        raise ValueError("copy object integrity absent")
    library,table=args.table.split('.')
    for index,event in enumerate(snapshot,1):
        if (event.get('journal_receiver')!='SNAPSHOT:'+args.copy_run_id or event.get('journal')!='SNAPSHOT:'+args.table or
                event.get('operation')!='c' or event.get('journal_sequence')!=index):raise ValueError("invalid snapshot epoch/ordinal")
    segments=journal.get('segments') if multi else [journal]
    if not isinstance(segments,list) or len(segments)!=len(windows):raise ValueError("Journal segment is omitted")
    events=[];actual=[]
    for segment,window in zip(segments,windows,strict=True):
        if segment.get('scan_complete') is not True or any(segment.get(k)!=window[k] for k in ('receiver','start','end')):
            raise ValueError("independent scan incomplete or outside the boundary")
        if multi and segment.get('receiver_library')!=window['receiver_library']:raise ValueError("foreign library segment")
        records=segment.get('records')
        if not isinstance(records,list) or type(segment.get('decoded'))is not int or segment['decoded']!=len(records):
            raise ValueError("independent event count incomplete")
        for event in records:
            sequence=int(n.normalize(event.get('journal_sequence'),'int'))
            if (event.get('journal_receiver')!=window['receiver'] or not window['start']<=sequence<=window['end'] or
                event.get('journal')!=args.journal_name):raise ValueError("journal event outside the window")
            actual.append((window['receiver'],sequence));events.append(event)
    if positions.get('kind')!='ibmi-source-rowpos-reader' or not isinstance(positions.get('positions'),list):
        raise ValueError('oracle ROWPOS absent')
    if multi:
        expected_chain=[{'JOURNAL_RECEIVER_LIBRARY':w['receiver_library'],'JOURNAL_RECEIVER_NAME':w['receiver']} for w in windows]
        if positions.get('receivers')!=expected_chain:raise ValueError("ROWPOS chain differs from the source catalog")
    expected=[];by_receiver={w['receiver']:w for w in windows}
    for row in positions['positions']:
        seq=int(n.normalize(row.get('SEQUENCE_NUMBER'),'int'));name=row.get('JOURNAL_RECEIVER_NAME')
        if name not in by_receiver:raise ValueError("foreign ROWPOS receiver")
        window=by_receiver[name]
        if window['start']<=seq<=window['end']:expected.append((name,seq))
    if len(set(expected))!=len(expected) or len(set(actual))!=len(actual) or set(expected)!=set(actual):
        raise ValueError("ROWPOS/image coverage incomplete or duplicated")
    history=[history_row(e,columns,library,table) for e in snapshot+events]
    if len(history)>n.MAX_ROWS:raise ValueError("total HISTORY budget exceeded")
    n.history_proof(history,history)
    return {'kind':'ibmi-journal-and-snapshot-oracle',**{k:getattr(args,k) for k in SCOPE},
            'copy_run_id':args.copy_run_id,'copy_boundary':args.copy_boundary,'events':history,
            'checkpoint':checkpoint,'copy_artifacts':copy['artifacts'],'source_journal_positions':positions,
            'journal_scan':({'scan_complete':True,'receiver_catalog':journal['receiver_catalog'],'segments':[{k:segment[k] for k in ('receiver','receiver_library','start','end','decoded','scan_complete')} for segment in segments]} if multi else {k:journal[k] for k in ('receiver','start','end','decoded','scan_complete')}),
            'observed_utc':n.utc(),'independent_of_snowflake':True,'native_cdc_qualified':False}


def bind_mutation_receipts(acks,oracle,pause,resume,before,args):
    # Le collecteur lie les ACK reçus à de vraies images source ; il ne
    # fabrique ni ACK, ni nombre de lignes affectées, ni positions journal.
    for item in (oracle,pause,resume,before):scope_matches(item,args)
    if (oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True or
            oracle.get('copy_run_id')!=args.copy_run_id or oracle.get('copy_boundary')!=args.copy_boundary or
            before.get('snapshot_checks_passed') is not True or pause.get('kind')!='native-reader-pause-proof' or
            pause.get('reader',{}).get('deployment_absent') is not True or pause['reader'].get('owned_pods')!=[] or
            resume.get('kind')!='native-reader-resume-proof' or resume.get('reader',{}).get('ready_replicas')!=1):
        raise ValueError("Oracle/pause/resume context is incomplete")
    return _bind_mutations(acks,oracle,before,args,pause['observed_utc'],resume['observed_utc'])


def bind_crash_mutation_receipts(acks,oracle,crash,before,args):
    for item in (oracle,crash,before):scope_matches(item,args)
    if (oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True or
            oracle.get('copy_run_id')!=args.copy_run_id or oracle.get('copy_boundary')!=args.copy_boundary or
            before.get('snapshot_checks_passed') is not True):
        raise ValueError("independent crash oracle incomplete")
    lower,upper=n.validate_crash_replacement(before,{**before,'reader':crash['reader'],'observed_utc':crash['observed_utc']},crash)
    return _bind_mutations(acks,oracle,before,args,lower.isoformat(),upper.isoformat())


def bind_rotation_mutation_receipts(acks,oracle,rotation,before,after,catalog_before,catalog_after,args):
    """Lie les ACK après rotation aux images du nouveau receiver, sans DML."""
    from ..java_catalog import parse_catalog_output
    for item in (oracle,rotation,before,after,catalog_before,catalog_after):scope_matches(item,args)
    if (oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True
            or oracle.get('copy_run_id')!=args.copy_run_id or oracle.get('copy_boundary')!=args.copy_boundary
            or oracle.get('checkpoint')!=after.get('checkpoint')
            or oracle.get('source_journal_positions',{}).get('positions')!=after.get('source_journal_positions',{}).get('positions')
            or before.get('snapshot_checks_passed') is not True or after.get('snapshot_checks_passed') is not True):
        raise ValueError("independent rotation oracle or snapshots incomplete")
    n.history_proof(oracle['events'],after['history'])
    rows=parse_catalog_output(catalog_after['output'])
    if not rows or rows[-1].first_sequence is None or rows[-1].receiver!=after['checkpoint']['receiver']:
        raise ValueError("new rotation boundary absent")
    _,lower=n.validate_receipt(rotation,before['observed_utc'])
    if not isinstance(acks,list) or any(n.validate_receipt(ack,before['observed_utc'])[0]<=lower for ack in acks):
        raise ValueError("mutation must start after the rotation ACK")
    receipts=_bind_mutations(acks,oracle,before,args,rotation['write_ack_utc'],after['observed_utc'],
                             receiver_window=(rows[-1].receiver,rows[-1].first_sequence-1))
    # Le contrat public complet valide catalogue, CONT, UID, ROWPOS et H/M
    # avant de rendre un binding ; aucune preuve partielle n'est publiée.
    n.evaluate_rotation(before,after,{'rotation':rotation,'catalog_before':catalog_before,'catalog_after':catalog_after,
        'mutations':receipts,'columns':args.columns if isinstance(args.columns,dict) else json.loads(Path(args.columns).read_text()),
        'pk':args.pk,'journal_library':args.journal_library,'journal_name':args.journal_name})
    return receipts


def _bind_mutations(acks,oracle,before,args,lower,upper,*,receiver_window=None):
    if not isinstance(acks,list) or not 1<=len(acks)<=20:raise ValueError("Mutation ACKs are missing")
    columns=args.columns if isinstance(args.columns,dict) else json.loads(Path(args.columns).read_text())
    events=oracle['events'];n.history_proof(events,events)
    index={(e['JOURNAL_RECEIVER'],int(e['JOURNAL_SEQUENCE'])):e for e in events}
    used=set();receipts=[]
    receiver,sequence_floor=(before['checkpoint']['receiver'],before['checkpoint']['sequence']) if receiver_window is None else receiver_window
    for ack in acks:
        scope_matches(ack,args)
        start,end=n.validate_receipt(ack,before['observed_utc'])
        if (not n.stamp(lower)<=start<=end<n.stamp(upper) or
                type(ack.get('affected_rows')) is not int or ack['affected_rows']!=1 or 'expected_events' in ack):
            raise ValueError("actual ACK during pause absent or declared images refused")
        positions=ack.get('source_positions');shape={'insert':['c'],'update':['u_before','u_after'],'delete':['d']}.get(ack.get('operation'))
        mutation=ack.get('mutation_identity')
        if positions is None and shape is not None and isinstance(mutation,dict):
            # Positions réellement relues, jamais déduites d'un simple tail.
            matches=[e for e in events if e.get('OPERATION')==shape[-1] and e.get('JOURNAL_RECEIVER')==receiver
                     and sequence_floor<int(e['JOURNAL_SEQUENCE'])<=oracle['checkpoint']['sequence']
                     and all(k in columns and n.normalize(e.get(k),columns[k])==n.normalize(v,columns[k]) for k,v in mutation.items())]
            if len(matches)!=1:raise ValueError("acknowledged source image absent or ambiguous")
            last=matches[0];sequence=int(last['JOURNAL_SEQUENCE']);receiver=last['JOURNAL_RECEIVER']
            if ack['operation']=='update':
                preceding=index.get((receiver,sequence-1))
                if preceding is None or preceding.get('OPERATION')!='u_before' or n.normalize(preceding.get(args.pk),columns[args.pk])!=n.normalize(mutation.get(args.pk),columns[args.pk]):
                    raise ValueError("source before/after pair not linked: exact entry required")
                positions=[{'receiver':receiver,'sequence':sequence-1},{'receiver':receiver,'sequence':sequence}]
            else:positions=[{'receiver':receiver,'sequence':sequence}]
        if shape is None or not isinstance(positions,list) or len(positions)!=len(shape):raise ValueError("Acknowledgment positions are missing")
        selected=[]
        for position in positions:
            key=(position['receiver'],int(n.normalize(position['sequence'],'int')))
            if (key in used or key not in index or key[0]!=receiver or key[1]<=sequence_floor or
                    key[0]!=oracle['checkpoint']['receiver'] or key[1]>oracle['checkpoint']['sequence']):
                raise ValueError("mutation position outside the resumed range or ambiguous")
            used.add(key);selected.append(index[key])
        if [e['OPERATION'] for e in selected]!=shape:raise ValueError("ACK operation differs from the source images")
        if any(not set(columns)<=set(event) for event in selected):
            raise ValueError("acknowledged source image columns absent")
        required={args.pk} if ack['operation']=='delete' else set(columns)
        if (not isinstance(mutation,dict) or set(mutation)!=required or
                any(n.normalize(e.get(args.pk),columns[args.pk])!=n.normalize(mutation[args.pk],columns[args.pk]) for e in selected)):
            raise ValueError("acknowledged mutation identity differs from the source images")
        if ack['operation']!='delete' and any(n.normalize(selected[-1].get(k),columns[k])!=
                                             n.normalize(mutation[k],columns[k]) for k in required):
            raise ValueError("acknowledged values differ from the independent source image")
        receipts.append({**ack,'source_positions':positions,'expected_events':selected,
                         'expected_events_pending':False,'source_positions_origin':'independent-journal-image+ROWPOS',
                         'source_oracle_observed_utc':oracle['observed_utc']})
    return receipts


RETAINED_SCOPE = (*SCOPE, 'database', 'schema', 'history', 'mirror', 'journal_library',
                  'journal_name', 'pk', 'identity', 'expected_role_arn',
                  'expected_gcp_service_account', 'expected_gcp_project', 'expected_gcs_bucket',
                  'expected_gcp_instance_id', 'expected_gcp_instance_name', 'expected_gcp_zone', 'expected_gcp_machine_type')
MAX_RETAINED_EPOCHS = 8


def same_retained_scope(old, current):
    if any(getattr(old, key, None) != getattr(current, key, None) for key in RETAINED_SCOPE):
        raise ValueError("retained epoch outside the scope")


def initial_copy_job(job, args, evidence_key):
    """Capture uniquement les champs réels nécessaires, jamais les secrets env."""
    metadata = job.get('metadata', {})
    labels = metadata.get('labels', {})
    annotations = metadata.get('annotations', {})
    status = job.get('status', {})
    containers = job.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
    selected = [c for c in containers if c.get('name') == 'initial-copy']
    if len(selected) != 1:
        raise ValueError("ambiguous initial-copy Job")
    container = selected[0]
    env = {e['name']: e['value'] for e in container.get('env', []) if 'value' in e}
    if (not metadata.get('uid') or not metadata.get('name') or not metadata.get('creationTimestamp') or
            labels.get('quadringent.io/pipeline-id') != args.pipeline or
            labels.get('quadringent.io/table-id') != args.table_id or
            labels.get('quadringent.io/component') != 'initial-copy' or
            annotations.get('quadringent.io/run-id') != args.copy_run_id or
            annotations.get('quadringent.io/bootstrap-boundary') != args.copy_boundary or
            env.get('AS400_EVIDENCE_KEY') != evidence_key or
            env.get('AS400_PIPELINE_ID') != args.pipeline or
            not isinstance(container.get('image'), str) or
            not container['image'].endswith('@' + getattr(args,'initial_copy_capture_digest',args.capture_digest)) or
            type(status.get('succeeded')) is not int or status['succeeded'] < 1 or status.get('failed', 0) or
            not any(c.get('type') == 'Complete' and c.get('status') == 'True' for c in status.get('conditions', []))):
        raise ValueError("retained Job outside the run, boundary, image or success")
    n.stamp(metadata['creationTimestamp'])
    return {'uid': metadata['uid'], 'name': metadata['name'], 'created_utc': metadata['creationTimestamp'],
            'pipeline': args.pipeline, 'table_id': args.table_id, 'run_id': args.copy_run_id,
            'boundary': args.copy_boundary, 'evidence_key': evidence_key, 'image': container['image'],
            'succeeded': status['succeeded'], 'complete': True}


def find_initial_copy_job(jobs, args, evidence_key):
    matches = [j for j in jobs if j.get('metadata', {}).get('annotations', {}).get('quadringent.io/run-id') == args.copy_run_id
               and j.get('metadata', {}).get('labels', {}).get('quadringent.io/table-id') == args.table_id]
    if len(matches) != 1 or matches[0].get('metadata',{}).get('namespace')!=args.namespace:
        raise ValueError("retained Job absent, duplicated or foreign namespace")
    return initial_copy_job(matches[0], args, evidence_key)


def pinned_archive_document(ref):
    """Référence privée fermée : les octets originaux restent l'autorité."""
    import stat
    if not isinstance(ref,dict) or set(ref)!={'path','sha256'} or not isinstance(ref['path'],str) or not isinstance(ref['sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',ref['sha256']) is None:
        raise ValueError("invalid archive reference")
    path=Path(ref['path'])
    if not path.is_absolute() or any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file() or stat.S_IMODE(path.stat().st_mode)&0o077:
        raise ValueError("regular private archive required")
    raw=path.read_bytes()
    if len(raw)>4_000_000 or hashlib.sha256(raw).hexdigest()!=ref['sha256']:
        raise ValueError("archive bytes changed")
    return json.loads(raw)


def archive_pointer(document,pointer):
    if not isinstance(pointer,list) or not 1<=len(pointer)<=12 or not all((isinstance(k,str) and k) or (type(k) is int and k>=0) for k in pointer):
        raise ValueError("Invalid anchor path")
    for key in pointer:
        if isinstance(document,dict) and isinstance(key,str) and key in document:document=document[key]
        elif isinstance(document,list) and type(key)is int and key<len(document):document=document[key]
        else:raise ValueError("Original field is missing")
    return document


def archived_initial_copy_job(args,evidence_key):
    ref=getattr(args,'initial_copy_job_archive',None)
    document=pinned_archive_document(ref)
    if document.get('kind')=='quadringent-job-api-projection-archive-v1':return projected_initial_copy_job(args,evidence_key,document)
    expected={'kind','scope','job_uid','api_document','observation_anchor','observed_utc_pointer','api_sha256_pointer'}
    v2=document.get('kind')=='quadringent-job-api-archive-v2'
    if v2:expected.add('api_document_pointer')
    if set(document)!=expected or document['kind'] not in {'quadringent-job-api-archive-v1','quadringent-job-api-archive-v2'} or document['scope']!={k:getattr(args,k) for k in SCOPE}:
        raise ValueError("archive outside the scope")
    original=pinned_archive_document(document['api_document']);anchor=pinned_archive_document(document['observation_anchor'])
    if v2 and document['api_sha256_pointer'] is None:
        if document['observation_anchor']!=document['api_document']:raise ValueError("direct anchor requires the same original bytes")
    elif archive_pointer(anchor,document['api_sha256_pointer'])!=document['api_document']['sha256']:
        raise ValueError("original anchor does not bind the API bytes")
    observed=archive_pointer(anchor,document['observed_utc_pointer']);observed_time=n.stamp(observed)
    if v2:
        original=archive_pointer(original,document['api_document_pointer'])
        if not isinstance(original,dict) or original.get('apiVersion')!='batch/v1' or original.get('kind') not in {'Job','JobList'}:raise ValueError("Complete original Job API response is required")
    items=original.get('items') if original.get('kind') in {'List','JobList'} else [original]
    if not isinstance(items,list):raise ValueError("Original API inventory is missing")
    scoped=[j for j in items if j.get('kind')=='Job' and j.get('metadata',{}).get('labels',{}).get('quadringent.io/pipeline-id')==args.pipeline and j.get('metadata',{}).get('labels',{}).get('quadringent.io/table-id')==args.table_id]
    runs=[j.get('metadata',{}).get('annotations',{}).get('quadringent.io/run-id') for j in scoped]
    if len(runs)!=len(set(runs)) or any(not isinstance(r,str) or not r for r in runs):raise ValueError("duplicate archived API runs")
    if successful_copy_runs(scoped,args)!=[args.copy_run_id]:raise ValueError("additional successful epoch in the archived API inventory")
    matches=[j for j in scoped if j['metadata'].get('uid')==document['job_uid']]
    if len(matches)!=1:raise ValueError("archived API UID absent or duplicated")
    raw_job=matches[0]
    if raw_job.get('apiVersion')!='batch/v1':raise ValueError("complete Job API version required")
    job=initial_copy_job(raw_job,args,evidence_key)
    status=raw_job['status'];ttl=raw_job['spec'].get('ttlSecondsAfterFinished')
    if raw_job['metadata'].get('namespace')!=args.namespace or type(ttl)is not int or not 1<=ttl<=604800 or status.get('succeeded')!=1 or status.get('failed',0):
        raise ValueError("invalid archive namespace, TTL or success")
    created=n.stamp(job['created_utc']);completed=n.stamp(status['completionTime'])
    if not created<=completed<=observed_time<completed+__import__('datetime').timedelta(seconds=ttl):
        raise ValueError("anchor outside the Job's original window")
    return job,raw_job,document,observed,completed+__import__('datetime').timedelta(seconds=ttl)


LIMITED_JOB_CLAIMS = {'historical_namespace_verified':False,'ttl_observed':False,
                      'historical_identity_verified':False,'old_jobs_inventory_complete':False}
ARCHIVED_JOB_SOURCES = {'archived_api','archived_api_projection_limited'}


def projected_initial_copy_job(args,evidence_key,document):
    """Projection authentique limitée aux données ; aucun champ API reconstruit."""
    required={'kind','scope','job_uid','api_document','observation_anchor','api_sha256_pointer'}
    if set(document)!=required or document['scope']!={k:getattr(args,k) for k in SCOPE}:
        raise ValueError("projection outside the current scope")
    original=pinned_archive_document(document['api_document'])
    anchor=pinned_archive_document(document['observation_anchor'])
    if archive_pointer(anchor,document['api_sha256_pointer'])!=document['api_document']['sha256']:
        raise ValueError("projection not anchored in the original bytes")
    if original.get('kind')!='public-native-initial-copy-export' or original.get('collector')!='quadringent.qualification.collect.LiveCollector.copy':
        raise ValueError("Original public projection is required")
    raw_job=original['job'];copy=original['copy'];job=initial_copy_job(raw_job,args,evidence_key)
    if job['uid']!=document['job_uid'] or any(copy.get(k)!=v for k,v in
        {'pipeline_id':args.pipeline,'table_id':args.table_id,'run_id':args.copy_run_id}.items()):
        raise ValueError("copy/Job projection mismatch")
    boundary=copy.get('boundary',{})
    if str(boundary.get('receiver_name'))+':'+str(boundary.get('last_sequence'))!=args.copy_boundary:
        raise ValueError("boundary projection mismatch")
    observed=original['observed_utc'];when=n.stamp(observed)
    if not n.stamp(job['created_utc'])<=n.stamp(raw_job['status']['completionTime'])<=when or job['succeeded']!=1:
        raise ValueError("projection observation predates success")
    return job,raw_job,document,observed,when


def historical_job_allowed(args):
    return bool(getattr(args,'initial_copy_revision',None) and args.initial_copy_revision!=args.expected_revision)


def job_binding(job):
    return {k:v for k,v in job.items() if k not in {'job_evidence_source','archive','api_absence','primary_reader_witness','archived_jobs_inventory_complete',*LIMITED_JOB_CLAIMS}}


def primary_archive_reader(probe,args):
    """Lecteur actuel admis ; frontière bootstrap sans inventer de run_id."""
    if not callable(getattr(probe,'workload',None)):raise ValueError("current primary reader required")
    reader,env=probe.workload(args.reader_deployment,args.capture_digest)
    n.workload_candidate(reader,'reader')
    if not reader.get('uid') or not reader.get('pod_uid'):raise ValueError("primary reader absent")
    library,table=args.table.split('.')
    entries=json.loads(env['AS400_TABLE_BOOTSTRAP_JSON'])
    matches=[t for t in entries if t.get('table_id')==args.table_id]
    if len(matches)!=1 or matches[0].get('schema')!=library or matches[0].get('table')!=table or matches[0].get('receiver')!=args.copy_boundary.rsplit(':',1)[0] or type(matches[0].get('sequence')) is not int or not 0<=matches[0]['sequence']<=int(args.copy_boundary.rsplit(':',1)[1]):
        raise ValueError("current bootstrap differs from the primary copy")
    if env.get('AS400_JOURNAL_LIBRARY')!=args.journal_library or env.get('AS400_JOURNAL_NAME')!=args.journal_name:
        raise ValueError("primary reader journal differs")
    return reader


def resolve_initial_copy_job(jobs,args,evidence_key,probe,*,sealed=False):
    """Pas de faux Job injecté dans l'inventaire : live ou archive explicitement lié."""
    matches=[j for j in jobs if j.get('metadata',{}).get('annotations',{}).get('quadringent.io/run-id')==args.copy_run_id and j.get('metadata',{}).get('labels',{}).get('quadringent.io/table-id')==args.table_id]
    if len(matches)>1:raise ValueError("duplicate current Job")
    archive=getattr(args,'initial_copy_job_archive',None)
    if archive is None:
        result=find_initial_copy_job(jobs,args,evidence_key)
        return {**result,'job_evidence_source':'live_api'}
    archived,raw_job,document,_,expires=archived_initial_copy_job(args,evidence_key)
    limited=document['kind']=='quadringent-job-api-projection-archive-v1'
    claims=LIMITED_JOB_CLAIMS if limited else {}
    current=probe.initial_copy_job_api(archived['name'])
    if current.get('kind')!='quadringent-job-api-not-found':
        if len(matches)!=1 or current!=matches[0] or initial_copy_job(current,args,evidence_key)!=archived:
            raise ValueError("Current Job API response contradicts its archive")
        # Les champs immuables et le statut terminé doivent correspondre ; resourceVersion peut changer.
        for key in (('uid','name','creationTimestamp','labels','annotations') if limited else ('uid','namespace','name','creationTimestamp','labels','annotations')):
            if current['metadata'].get(key)!=raw_job['metadata'].get(key):raise ValueError('Job live/archive divergent')
        if (not limited and current['spec']!=raw_job['spec']) or current['status']!=raw_job['status']:raise ValueError('Job live/archive divergent')
        return {**archived,'job_evidence_source':'live_api','archive':archive,**claims}
    if matches:raise ValueError("Missing GET contradicts the API inventory")
    primary=not sealed and not historical_job_allowed(args)
    if (primary and limited) or current.get('name')!=archived['name'] or current.get('namespace')!=args.namespace or n.stamp(current['observed_utc'])<expires:
        raise ValueError("Historical absence cannot be admitted")
    witness=primary_archive_reader(probe,args) if primary else None
    if primary:
        assert witness is not None
        if current.get('namespace_witness')!={'deployment':args.reader_deployment,'uid':witness['uid']}:raise ValueError("API absence without an identical current-reader witness")
    return {**archived,'job_evidence_source':'archived_api_projection_limited' if limited else 'archived_api','archive':archive,'api_absence':current,**claims,
            **({'primary_reader_witness':witness} if primary else {}),
            **({'archived_jobs_inventory_complete':False} if document.get('kind')=='quadringent-job-api-archive-v2' and raw_job.get('kind')=='Job' else {})}


def inventory_matches(jobs,args,expected_runs,archived_runs=()):
    """Compare séparément les Jobs réels et les anciennes absences validées."""
    actual=successful_copy_runs(jobs,args)
    missing=set(expected_runs)-set(actual)
    if len(expected_runs)!=len(set(expected_runs)) or set(actual)-set(expected_runs) or missing-set(archived_runs):
        raise ValueError("Job inventory contains an omitted epoch or an absent epoch without an archive")


def freeze_initial_copy_job(probe,jobs,args,evidence_key,out):
    """Fige une observation API réelle avant TTL pour sa revalidation ultérieure."""
    evidence=resolve_initial_copy_job(jobs,args,evidence_key,probe)
    if getattr(args,'initial_copy_job_archive',None) is not None:return evidence,args.initial_copy_job_archive
    actual=probe.initial_copy_job_api(evidence['name'])
    if actual.get('kind')!='Job' or initial_copy_job(actual,args,evidence_key)!=job_binding(evidence):raise ValueError("Job changed before capture")
    api_path=Path(out)/'retain-history.job-api.json';n.write_private(api_path,actual)
    api_ref={'path':str(api_path.resolve()),'sha256':hashlib.sha256(api_path.read_bytes()).hexdigest()}
    observed_path=Path(out)/'retain-history.job-observation.json';n.write_private(observed_path,{'kind':'quadringent-job-api-observation-v1','observed_utc':n.utc(),'api_document_sha256':api_ref['sha256']})
    anchor={'path':str(observed_path.resolve()),'sha256':hashlib.sha256(observed_path.read_bytes()).hexdigest()}
    archive_path=Path(out)/'retain-history.job-archive.json';n.write_private(archive_path,{'kind':'quadringent-job-api-archive-v1','scope':{k:getattr(args,k) for k in SCOPE},'job_uid':evidence['uid'],'api_document':api_ref,'observation_anchor':anchor,'observed_utc_pointer':['observed_utc'],'api_sha256_pointer':['api_document_sha256']})
    ref={'path':str(archive_path.resolve()),'sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest()}
    args.initial_copy_job_archive=ref;archived_initial_copy_job(args,evidence_key)
    return {**evidence,'archive':ref},ref


def successful_copy_runs(jobs, args):
    runs = []
    for job in jobs:
        meta = job.get('metadata', {})
        labels = meta.get('labels', {})
        if (labels.get('quadringent.io/pipeline-id') == args.pipeline and
                labels.get('quadringent.io/table-id') == args.table_id and
                labels.get('quadringent.io/component') == 'initial-copy' and job.get('status', {}).get('succeeded', 0)):
            run = meta.get('annotations', {}).get('quadringent.io/run-id')
            if not isinstance(run, str) or not run or run in runs:
                raise ValueError("inconsistent epoch inventory")
            runs.append(run)
    return sorted(runs)


def validate_retained_parts(parts, args, columns):
    """Redécode les octets journal figés, puis la couverture ROWPOS complète."""
    from quadringent.raw import read_raw_batch
    journal = parts['journal']
    positions = parts['positions']
    rowpos_bytes=base64.b64decode(positions.get('rowpos_b64',''),validate=True)
    if not rowpos_bytes or len(rowpos_bytes)>2000000:
        raise ValueError("retained ROWPOS bytes absent or over budget")
    lines=rowpos_bytes.decode('utf-8').splitlines()
    rows=[json.loads(line[len('SRC_ROWPOS='):]) for line in lines if line.startswith('SRC_ROWPOS=')]
    counts=[line for line in lines if line.startswith('SRC_ROWPOS_COUNT=')]
    receivers=[json.loads(line[len('SRC_RECEIVER='):]) for line in lines if line.startswith('SRC_RECEIVER=')]
    if (receivers!=positions.get('receivers',[]) or rows != positions.get('positions') or counts != ['SRC_ROWPOS_COUNT='+str(len(rows))] or
            len(rows)>n.MAX_ROWS or len(lines)!=len(rows)+len(receivers)+1):
        raise ValueError("retained ROWPOS bytes changed or incomplete")
    segments=journal.get('segments') if journal.get('kind')=='ibmi-multi-receiver-journal' else [journal]
    if not isinstance(segments,list) or not 1<=len(segments)<=8:raise ValueError("retained segments absent")
    total_batches=0
    for segment in segments:
        batches=segment.get('raw_batches')
        if not isinstance(batches,list):raise ValueError("retained journal bytes absent")
        total_batches+=len(batches)
        if total_batches>100:raise ValueError("retained batches over budget")
        records: list[dict] = []
        for batch in batches:
            if set(batch)!={'manifest_b64','payload_b64'}:raise ValueError("invalid retained journal batch")
            payload=base64.b64decode(batch['payload_b64'],validate=True)
            manifest=base64.b64decode(batch['manifest_b64'],validate=True)
            if len(payload)>2000000 or len(manifest)>262144:raise ValueError("retained journal batch over budget")
            decoded=read_raw_batch(manifest,payload,preserve_decimals=True)
            if decoded.manifest.high_watermark.receiver!=segment['receiver'] or decoded.manifest.high_watermark.sequence!=segment['end']:
                raise ValueError("journal bytes outside the boundary")
            records.extend(e.to_record() for e in decoded.events)
        if json.loads(json.dumps(records,default=str))!=segment['records']:
            raise ValueError("retained journal changed or bytes omitted")
    return validate_oracle_parts(parts['copy'], journal, parts['positions'], parts['checkpoint'], args, columns)


def union_history_oracles(current, retained):
    """Union d'oracles indépendants ; ne lit et ne déduit jamais HISTORY Snow."""
    if not 1 <= len(retained) <= MAX_RETAINED_EPOCHS:
        raise ValueError("retained epochs absent or over budget")
    epochs = set()
    indexed: dict[str, dict] = {}
    positions = {}
    for oracle in [*retained, current]:
        if (oracle.get('kind') != 'ibmi-journal-and-snapshot-oracle' or
                oracle.get('independent_of_snowflake') is not True or
                any(oracle.get(k) != current.get(k) for k in SCOPE)):
            raise ValueError("retained oracle outside the scope")
        epoch = oracle.get('copy_run_id')
        if not epoch or epoch in epochs:
            raise ValueError("duplicate epoch")
        epochs.add(epoch)
        n.history_proof(oracle['events'], oracle['events'])
        for row in oracle['events']:
            event = row['EVENT_ID']
            position = (row['JOURNAL_RECEIVER'], str(row['JOURNAL_SEQUENCE']))
            if event in indexed:
                # Un journal déjà capturé peut apparaître dans deux fenêtres.
                # Les snapshots doivent appartenir à des époques distinctes.
                if row['JOURNAL_RECEIVER'].startswith('SNAPSHOT:') or indexed[event] != row:
                    raise ValueError("duplicate or contradictory retained event")
                continue
            if position in positions:
                raise ValueError("contradictory retained position")
            indexed[event] = row
            positions[position] = event
    if len(indexed) > n.MAX_ROWS:
        raise ValueError("multi-epoch history over budget")
    events = list(indexed.values())
    n.history_proof(events, events)
    return {**current, 'events': events, 'retained_epochs': [o['copy_run_id'] for o in retained],
            'history_oracle_mode': 'authenticated_retained_epochs', 'native_cdc_qualified': False}


class LiveCollector:
    def __init__(self,probe):self.probe=probe;self.args=probe.args
    def copy(self,loader,*,args=None,evidence_key=None):
        a=args or self.args;p=self.probe
        evidence_expression=repr(evidence_key) if evidence_key is not None else "t.evidence_key"
        # Utilise les mêmes octets immuables que le Job, jamais HISTORY.
        script=f'''import hashlib,json,os,sys
{p.loader_import_script("l")}
from quadringent.storage_backend import StorageBackend
from quadringent.storage_layout import snapshot_prefix
from quadringent.raw import read_raw_batch
tables=[t for t in l.parse_table_set(os.environ['QUADRINGENT_LOADER_TABLE_SET_JSON']) if t.table_id=={a.table_id!r}]
if len(tables)!=1:raise SystemExit(1)
t=tables[0];plan=l.build_plan(t,database={a.database!r},schema={a.schema!r})
if (os.environ['QUADRINGENT_DESTINATION_DATABASE']!={a.database!r} or os.environ['QUADRINGENT_DESTINATION_SCHEMA']!={a.schema!r} or
 t.schema_name.upper()!={p.library!r} or t.table_name.upper()!={p.table!r} or list(t.key_columns)!=[{a.pk!r}] or
 [x['name'] for x in t.columns]!={list(p.columns)!r} or plan.history_table!={a.history!r} or plan.mirror_table!={a.mirror!r}):raise SystemExit(1)
groups={{'int':{{'int','integer','smallint','bigint'}},'decimal':{{'decimal','numeric','decfloat'}},'text':{{'char','varchar','clob'}},'date':{{'date'}},'timestamp':{{'timestamp'}}}}
if any(str(x['kind']).lower() not in groups[{p.columns!r}[x['name']]] for x in t.columns):raise SystemExit(1)
storage=StorageBackend.from_environment(os.environ);root=storage.object_store('')
evidence_key={evidence_expression}
raw=root.get_bounded(evidence_key,262144);evidence=json.loads(raw)
if evidence.get('pipeline_id')!={a.pipeline!r} or evidence.get('table_id')!={a.table_id!r} or evidence.get('run_id')!={a.copy_run_id!r}:raise SystemExit(1)
batches=evidence.get('snapshot_batches')
if not isinstance(batches,list) or not 1<=len(batches)<=100:raise SystemExit(1)
store=storage.object_store(snapshot_prefix(os.environ.get('AS400_RAW_PREFIX',''),t.table_name))
records=[];artifacts=[];keys=set()
for ref in batches:
 if set(ref)!={{'payload_key','manifest_key'}}:raise SystemExit(1)
 for key in ref.values():
  if not isinstance(key,str) or '/' in key or not key.startswith('batch-') or key in keys:raise SystemExit(1)
  keys.add(key)
 payload=store.get_bounded(ref['payload_key'],2000000);manifest=store.get_bounded(ref['manifest_key'],262144)
 batch=read_raw_batch(manifest,payload,preserve_decimals=True)
 if batch.manifest.high_watermark.receiver!='SNAPSHOT:'+{a.copy_run_id!r} or batch.manifest.high_watermark.sequence!=max(e.position.sequence for e in batch.events):raise SystemExit(1)
 records.extend(e.to_record() for e in batch.events)
 if len(records)>1000:raise SystemExit(1)
 artifacts.append({{**ref,'verified':True,'payload_sha256':hashlib.sha256(payload).hexdigest(),'manifest_sha256':hashlib.sha256(manifest).hexdigest(),'events':len(batch.events)}})
evidence.update(records=records,artifacts=[{{'evidence_key':evidence_key,'verified':True,'sha256':hashlib.sha256(raw).hexdigest()}},*artifacts])
print(json.dumps(evidence,default=str))
'''
        return p.exec_json(loader[0]['pod'],loader[0]['container'],script)

    def receiver_catalog(self,reader,copy):
        a=self.args;p=self.probe;b=copy['boundary']
        command=['java','-cp','/app/probe.jar:/app/lib/*','io.quadringent.as400.ReadOnlyReceiverCatalog']
        script=f'''import hashlib,json,os,subprocess
from pathlib import Path
from datetime import datetime,timezone
if os.environ['AS400_JOURNAL_LIBRARY']!={a.journal_library!r} or os.environ['AS400_JOURNAL_NAME']!={a.journal_name!r}:raise SystemExit(1)
env=dict(os.environ);env.update(AS400_RECEIVER_METADATA_LIMIT='9',AS400_CATALOG_REQUIRES_RECEIVER={b['receiver_name']!r},AS400_CATALOG_QUERY_TIMEOUT_SECONDS='10')
env.pop('AS400_OBJECT_LIBRARY',None)
run=subprocess.run({command!r},env=env,text=True,capture_output=True,timeout=25)
if run.returncode or len(run.stdout.encode())>8192:raise SystemExit(1)
print(json.dumps({{'kind':'ibmi-source-receiver-catalog',**{ {k:getattr(a,k) for k in SCOPE}!r},'journal_library':{a.journal_library!r},'journal_name':{a.journal_name!r},'observed_utc':datetime.now(timezone.utc).isoformat(),'output':run.stdout,'output_sha256':hashlib.sha256(run.stdout.encode()).hexdigest(),'jar_sha256':hashlib.sha256(Path('/app/probe.jar').read_bytes()).hexdigest()}},default=str))
'''
        result=p.exec_json(reader[0]['pod'],reader[0]['container'],script)
        result['reader']=reader[0]
        return result

    def journal(self,reader,copy,checkpoint):
        if checkpoint['receiver']==copy['boundary']['receiver_name']:
            return self._journal_single(reader,copy,checkpoint)
        catalog=self.receiver_catalog(reader,copy)
        windows=receiver_windows(copy['boundary'],checkpoint,catalog,self.args)
        segments=[]
        for window in windows:
            segment_copy={'boundary':{'receiver_name':window['receiver'],'receiver_library':window['receiver_library'],'last_sequence':window['start']-1}}
            segment=self._journal_single(reader,segment_copy,{'receiver':window['receiver'],'sequence':window['end']})
            segment['receiver_library']=window['receiver_library'];segments.append(segment)
        return {'kind':'ibmi-multi-receiver-journal','receiver_catalog':catalog,'segments':segments}

    def _journal_single(self,reader,copy,checkpoint):
        a=self.args;p=self.probe;b=copy['boundary'];low=b['last_sequence'];high=checkpoint['sequence']
        if checkpoint['receiver']!=b['receiver_name'] or not 0<=high-low<=1000:raise ValueError("rotation or window budget exceeded")
        if high==low:return {'receiver':b['receiver_name'],'start':low+1,'end':high,'decoded':0,'scan_complete':True,'records':[],'raw_batches':[]}
        request={'cmd':'sql_window','receiver':b['receiver_name'],'receiver_library':b['receiver_library'],'start_sequence':str(low+1),
                 'end_sequence':str(high),'max_server_entries':high-low,'max_decoded_entries':1000,'high_watermark_sequence':str(high)}
        command=['java','-cp','/app/probe.jar:/app/lib/*','io.quadringent.as400.PersistentJournalWorker']
        script=f'''import base64,json,os,re,subprocess,sys,tempfile
from pathlib import Path
from quadringent.raw import read_raw_batch
bootstrap=json.loads(os.environ['AS400_TABLE_BOOTSTRAP_JSON'])
if os.environ['ISERIES_SCHEMA'].upper()!={p.library!r} or not any(t['table_id']=={a.table_id!r} and t['schema'].upper()=={p.library!r} and t['table'].upper()=={p.table!r} for t in bootstrap):raise SystemExit(1)
env=dict(os.environ);env.update(ISERIES_SCHEMA={p.library!r},ISERIES_TABLE={p.table!r},ISERIES_TABLES={p.table!r},AS400_SQL_QUERY_TIMEOUT_SECONDS='10',AS400_SOCKET_TIMEOUT_MS='15000',AS400_RETRIEVE_TIMEOUT_MS='15000')
with tempfile.TemporaryDirectory(prefix='quadringent-readonly-oracle-') as directory:
 request={request!r};request['raw_directory']=directory
 run=subprocess.run({command!r},input=json.dumps(request)+'\\n'+json.dumps({{'cmd':'shutdown'}})+'\\n',env=env,text=True,capture_output=True,timeout=25)
 lines=run.stdout.splitlines()
 summaries=[x for x in lines if x.startswith('summary ')]
 if run.returncode or lines.count('worker_ready')!=1 or lines.count('window_done')!=1 or len(summaries)!=1 or any(x.startswith(('window_error=','connect_error=')) for x in lines):raise SystemExit(1)
 summary=dict(re.findall(r'(\\w+)=([^ ]+)',summaries[0]))
 if summary.get('scan_complete')!='true':raise SystemExit(1)
 records=[];hashes=[];raw_batches=[]
 for manifest in sorted(Path(directory).glob('batch-*.manifest.json')):
  payload=manifest.with_name(manifest.name.replace('.manifest.json','.jsonl'))
  manifest_bytes=manifest.read_bytes();payload_bytes=payload.read_bytes()
  batch=read_raw_batch(manifest_bytes,payload_bytes,preserve_decimals=True)
  raw_batches.append({{'manifest_b64':base64.b64encode(manifest_bytes).decode(),'payload_b64':base64.b64encode(payload_bytes).decode()}})
  if batch.manifest.high_watermark.receiver!=request['receiver'] or batch.manifest.high_watermark.sequence!=int(request['end_sequence']):raise SystemExit(1)
  records.extend(e.to_record() for e in batch.events);hashes.append(batch.manifest.payload_sha256)
 if int(summary['decoded'])!=len(records) or len(records)>1000:raise SystemExit(1)
 print(json.dumps({{'receiver':request['receiver'],'start':int(request['start_sequence']),'end':int(request['end_sequence']),
 'decoded':len(records),'scan_complete':True,'records':records,'payload_sha256':hashes,'raw_batches':raw_batches}},default=str))
'''
        return p.exec_json(reader[0]['pod'],reader[0]['container'],script)

    def positions(self,reader,copy):
        a=self.args;p=self.probe;b=copy['boundary']
        command=['java','-cp','/app/probe.jar:/app/lib/*','io.quadringent.as400.QualificationSourceDriver','rowpos',
                 p.library,p.table,a.journal_library,a.journal_name,a.pk,','.join(p.columns),
                 b['receiver_name'],str(b['last_sequence'])]
        script=f'''import base64,json,os,subprocess
stdin='\\n'.join(os.environ[k] for k in ('ISERIES_HOST','ISERIES_USER','ISERIES_PASSWORD'))+'\\n'
run=subprocess.run({command!r},input=stdin,text=True,capture_output=True,timeout=25)
if run.returncode:raise SystemExit(1)
lines=[line for line in run.stdout.splitlines() if line.startswith(('SRC_ROWPOS=','SRC_ROWPOS_COUNT=','SRC_RECEIVER='))]
rows=[json.loads(line[len('SRC_ROWPOS='):]) for line in lines if line.startswith('SRC_ROWPOS=')]
receivers=[json.loads(line[len('SRC_RECEIVER='):]) for line in lines if line.startswith('SRC_RECEIVER=')]
counts=[line for line in lines if line.startswith('SRC_ROWPOS_COUNT=')]
if counts != ['SRC_ROWPOS_COUNT='+str(len(rows))] or len(rows)>1000:raise SystemExit(1)
raw=('\\n'.join(lines)+'\\n').encode()
print(json.dumps({{'kind':'ibmi-source-rowpos-reader','positions':rows,'receivers':receivers,'rowpos_b64':base64.b64encode(raw).decode()}}))
'''
        return p.exec_json(reader[0]['pod'],reader[0]['container'],script)

    def oracle(self,*,capture_parts=False):
        p=self.probe;p.deadline=min(getattr(p,"deadline",float("inf")),time.monotonic()+self.args.max_seconds)
        reader=p.workload(self.args.reader_deployment,self.args.capture_digest)
        loader=p.workload(self.args.loader_deployment,self.args.loader_digest)
        copy=self.copy(loader)
        checkpoint=p.checkpoint(reader,loader)
        if not checkpoint:raise ValueError('checkpoint absent')
        positions=self.positions(reader,copy)
        journal=self.journal(reader,copy,checkpoint)
        after=p.checkpoint(reader,loader)
        if after!=checkpoint:raise ValueError("checkpoint moved during the oracle; retry when the source is idle")
        report=validate_oracle_parts(copy,journal,positions,checkpoint,self.args,p.columns)
        report.update(reader=reader[0],loader=loader[0])
        if capture_parts:
            report['retained_parts']={'copy':copy,'journal':journal,'positions':positions,'checkpoint':checkpoint}
        if time.monotonic()>p.deadline:raise RuntimeError('native_probe_deadline_exceeded')
        return report
