"""The installed port keeps selected secrets inside the worker input channel."""
from copy import deepcopy
from unittest.mock import Mock
import hashlib

import pytest

from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services import native_observer as module
from quadringent_control_plane.v2.services.native_context import canonical,context_snapshot
from quadringent_control_plane.v2.services.native_verifications import NativeArtifact
from test_v2_native_verifications import authority,Preparer  # noqa: F401


@pytest.fixture
def installed(authority,tmp_path):
    engine=authority[0];box=SecretBox(SecretBox.generate_key())
    with engine.begin() as connection:
        connection.execute(schema.sources.update().where(schema.sources.c.id=='source-one')
            .values(secret_ciphertext=box.encrypt('SYNTHETIC_WORKER_PASSWORD')))
        connection.execute(schema.destinations.update().where(schema.destinations.c.id=='destination-one')
            .values(key_pair_ciphertext=box.encrypt('SYNTHETIC_WORKER_PRIVATE_KEY')))
        _,context,_,_=context_snapshot(connection,'org-one','pipeline-one')
    prepared=Preparer().prepare(context)
    runtime={'java':'/server/java','classpath':'/server/reader.jar','files':{'/server/reader.jar':'a'*64}}
    observer=module.InstalledNativeObserver(engine,box,storage=StorageBackend('local','synthetic',str(tmp_path)),
        root='raw/test',org_id='org-one',runtime=runtime,producer_revision='a'*40,max_seconds=60)
    manifest=NativeArtifact('collection_manifest','local://synthetic/collection','a'*64,'a'*64,1).to_dict()
    report={'scope_sha256':prepared.scope.sha256}
    result={'collection':{'report':report,'manifest':manifest,'artifacts':[manifest]},
        'replay':{'comparison':report,'streams_replayed':7,'state':'incomplete','native_admission':'not_evaluated',
            'native_cdc_qualified':False,'collection_manifest':manifest,'scope_sha256':prepared.scope.sha256,
            'context_sha256':hashlib.sha256(canonical(context).encode()).hexdigest(),
            'missing_authorities':['platform','source_provenance','receiver_chain','physical_inventory','stable_cut',
                                   'destination_identity'],'validated_observations':['native_source_observations']}}
    return observer,prepared,context,result


def test_selected_credentials_are_not_command_arguments_or_environment(installed,monkeypatch):
    observer,prepared,context,result=installed
    calls=[]
    def worker(command,payload,**options):
        calls.append((command,deepcopy(payload),options))
        assert payload['secrets']['password']=='SYNTHETIC_WORKER_PASSWORD'
        assert payload['secrets']['private_key']=='SYNTHETIC_WORKER_PRIVATE_KEY'
        for secret in payload['secrets'].values():
            if secret:assert all(secret not in word for word in command) and secret not in str(options['environment'])
        return result
    monkeypatch.setattr(module,'run_worker',worker)
    monkeypatch.setenv('UNRELATED_SECRET','SYNTHETIC_UNRELATED_SECRET')
    collection,replay=observer.observe(prepared,context=context,check_current=lambda:True)
    assert len(calls)==1 and replay.state=='incomplete' and collection.manifest==replay.collection_manifest
    assert 'UNRELATED_SECRET' not in calls[0][2]['environment']
    from pathlib import Path
    assert not Path(calls[0][1]['directory']).exists()


@pytest.mark.parametrize('fault',['scope','context','manifest','admission'])
def test_worker_cannot_return_another_attempt_or_admitted_result(installed,monkeypatch,fault):
    observer,prepared,context,result=installed
    if fault=='scope':result['replay']['scope_sha256']='b'*64
    if fault=='context':result['replay']['context_sha256']='b'*64
    if fault=='manifest':result['replay']['collection_manifest']={**result['replay']['collection_manifest'],'sha256':'b'*64}
    if fault=='admission':result['replay']['native_cdc_qualified']=True
    monkeypatch.setattr(module,'run_worker',lambda *a,**kw:result)
    with pytest.raises(module.NativeObserverProcessError,match='native_observation_failed'):
        observer.observe(prepared,context=context,check_current=lambda:True)


def test_false_fence_prevents_configuration_and_worker_io(installed,monkeypatch):
    observer,prepared,context,_=installed
    configuration=Mock(side_effect=AssertionError('configuration opened'))
    worker=Mock(side_effect=AssertionError('worker started'))
    monkeypatch.setattr(observer,'_configuration',configuration);monkeypatch.setattr(module,'run_worker',worker)
    with pytest.raises(module.NativeObserverProcessError):observer.observe(prepared,context=context,check_current=lambda:False)
    configuration.assert_not_called();worker.assert_not_called()


def test_installed_entrypoint_is_not_shadowed_by_a_launcher(installed, monkeypatch, tmp_path):
    import subprocess

    observer, prepared, context, result = installed
    (tmp_path / "quadringent_control_plane.py").write_text("raise AssertionError('launcher shadowed package')")
    monkeypatch.chdir(tmp_path)

    def worker(command, payload, **options):
        # Invalid input must reach the worker's quiet refusal, without importing the launcher.
        completed = subprocess.run(command, input=b"{}\n", env=options["environment"], capture_output=True, timeout=10)
        assert completed.returncode == 1 and completed.stdout == completed.stderr == b""
        return result

    monkeypatch.setattr(module, "run_worker", worker)
    observer.observe(prepared, context=context, check_current=lambda: True)


def single_org(engine):
    from sqlalchemy import delete
    with engine.begin() as connection:
        for table,identifier in ((schema.pipelines,'pipeline-other'),(schema.tables,'table-other'),
                                 (schema.destinations,'destination-other'),(schema.sources,'source-other'),
                                 (schema.organizations,'org-other')):
            connection.execute(delete(table).where(table.c.id==identifier))


def test_explicit_upgrade_enables_tracking_and_supersedes_old_contexts(authority):
    from sqlalchemy import select
    from quadringent_control_plane.v2.entrypoint import ensure_organization
    engine=authority[0];single_org(engine)
    with engine.begin() as connection:
        connection.execute(schema.organizations.update().values(native_context_enforced=False))
        previous=dict(connection.execute(select(schema.pipelines.c.id,schema.pipelines.c.native_context_generation)).all())
    ensure_organization(engine,'org-one',native_context_tracking=True)
    with engine.connect() as connection:
        assert connection.execute(select(schema.organizations.c.native_context_enforced)).scalar_one() is True
        current=dict(connection.execute(select(schema.pipelines.c.id,schema.pipelines.c.native_context_generation)).all())
    assert current=={name:value+1 for name,value in previous.items()}


def test_legacy_disabled_tracking_is_preserved_without_explicit_upgrade(authority):
    from sqlalchemy import select
    from quadringent_control_plane.v2.entrypoint import ensure_organization
    engine=authority[0];single_org(engine)
    with engine.begin() as connection:connection.execute(schema.organizations.update().values(native_context_enforced=False))
    ensure_organization(engine,'org-one')
    with engine.connect() as connection:
        assert connection.execute(select(schema.organizations.c.native_context_enforced)).scalar_one() is False


def test_pending_operation_prevents_tracking_upgrade(authority):
    from sqlalchemy import select
    from quadringent_control_plane.v2.entrypoint import ConfigurationError,ensure_organization
    engine=authority[0];single_org(engine)
    with engine.begin() as connection:
        connection.execute(schema.organizations.update().values(native_context_enforced=False))
        connection.execute(schema.pipelines.update().where(schema.pipelines.c.id=='pipeline-one')
            .values(native_context_operation_id='pending-operation'))
    with pytest.raises(ConfigurationError):ensure_organization(engine,'org-one',native_context_tracking=True)
    with engine.connect() as connection:
        assert connection.execute(select(schema.organizations.c.native_context_enforced)).scalar_one() is False
        assert connection.execute(select(schema.pipelines.c.native_context_operation_id)
            .where(schema.pipelines.c.id=='pipeline-one')).scalar_one()=='pending-operation'


def test_existing_context_tracking_is_never_disabled_by_startup(authority):
    from quadringent_control_plane.v2.entrypoint import ConfigurationError,ensure_organization
    engine=authority[0];single_org(engine)
    with pytest.raises(ConfigurationError):ensure_organization(engine,'org-one',native_context_tracking=False)
