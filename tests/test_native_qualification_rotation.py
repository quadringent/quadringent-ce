"""Rotation hors connexion : aucun catalogue ou événement runtime synthétisé."""
from copy import deepcopy
import hashlib

import pytest

from quadringent.qualification import proof as p
import test_native_qualification_proof as fixtures


def fixture():
    before, after, ops = fixtures.PureProofTests().resume_fixture()
    for snapshot in (before, after):
        for key in ('reader', 'loader'):
            workload = snapshot[key]
            digest = workload['images'][0]['image_id'].split('://')[1]
            workload['image_binding'] = {'verified': True, 'observed_digest': digest,
                                         'index': digest, 'architecture': 'amd64', 'manifest': digest, 'config': digest, 'revision': 'a'*40, 'receipt_sha256': 'b'*64}
        snapshot.update(source=[{'ID': 1}], mirror=[{'ID': 1}])
    after.update(source=[{'ID':1},{'ID':2}],mirror=[{'ID':1},{'ID':2}])
    after['reader'] = deepcopy(before['reader'])
    after['checkpoint'] = {'receiver': 'R2', 'sequence': 11}
    event = after['history'][1]
    event.update(JOURNAL_RECEIVER='R2', JOURNAL_SEQUENCE=11)
    after['source_journal_positions']['positions'] = [{'JOURNAL_RECEIVER_NAME': 'R2', 'SEQUENCE_NUMBER': 11}]
    mutation = ops['mutations'][0]
    mutation.update(write_started_utc='2026-09-30T10:00:03+00:00', write_ack_utc='2026-09-30T10:00:04+00:00', expected_events=[event])
    scope = {k: before[k] for k in ('pipeline', 'table', 'table_id', 'namespace', 'context')}
    rotation = {**scope, 'journal_library':'S', 'journal_name':'J', 'kind': 'ibmi-journal-rotation-ack', 'attempts': 1, 'acknowledged': True, 'sequence_option': 'CONT',
                'write_started_utc': '2026-09-30T10:00:01+00:00', 'write_ack_utc': '2026-09-30T10:00:02+00:00'}
    catalog = {**scope, 'kind': 'ibmi-source-receiver-catalog', 'journal_library': 'S', 'journal_name': 'J'}
    cb = {**catalog, 'observed_utc': before['observed_utc'], 'output': 'as400-receiver-catalog-v2\nreceiver\tS\tR1\tATTACHED\t1\t10\t-\t-\t-\t-\n'}
    ca = {**catalog, 'observed_utc': '2026-09-30T10:00:02+00:00', 'output': 'as400-receiver-catalog-v2\nreceiver\tS\tR1\tONLINE\t1\t10\t-\t-\tS\tR2\nreceiver\tS\tR2\tATTACHED\t11\t11\tS\tR1\t-\t-\n'}
    for catalog,snapshot in ((cb,before),(ca,after)):
        catalog.update(output_sha256=hashlib.sha256(catalog['output'].encode()).hexdigest(),jar_sha256='c'*64,reader=deepcopy(snapshot['reader']))
    return before, after, {'rotation': rotation, 'catalog_before': cb, 'catalog_after': ca,
                           'journal_library':'S','journal_name':'J','mutations': [mutation], 'columns': {'ID': 'int'}, 'pk': 'ID'}


def test_rotation_retains_history_and_runtime():
    before, after, operations = fixture()
    result = p.evaluate_rotation(before, after, operations)
    assert result['rotation_qualified'] is True
    assert result['native_cdc_qualified'] is False
    assert result['maximum_10s_qualified'] is False


@pytest.mark.parametrize('change', [
    lambda b, a, o: o['rotation'].update(attempts=2),
    lambda b, a, o: o['rotation'].update(sequence_option='RESET'),
    lambda b, a, o: o['catalog_after'].update(output=o['catalog_after']['output'].replace('R2\tATTACHED\t11\t11', 'R2\tATTACHED\t1\t11')),
    lambda b, a, o: o['catalog_after'].update(output=o['catalog_after']['output'].replace('ONLINE', '-')),
    lambda b, a, o: o['rotation'].update(acknowledged=False),
    lambda b, a, o: o['rotation'].update(table_id='foreign'),
    lambda b, a, o: o['catalog_after'].update(output=o['catalog_before']['output']),
    lambda b, a, o: o['catalog_after'].update(journal_name='FOREIGN'),
    lambda b, a, o: o['catalog_after'].update(observed_utc=b['observed_utc']),
    lambda b, a, o: a['history'].pop(0),
    lambda b, a, o: a['history'][0].update(NOTE='changed'),
    lambda b, a, o: a['history'].append(deepcopy(a['history'][1])),
    lambda b, a, o: a['reader'].update(pod_uid='replacement'),
    lambda b, a, o: a['loader']['image_binding'].update(verified=False),
    lambda b, a, o: a['mirror'][0].update(ID=9),
    lambda b, a, o: a['source_journal_positions']['positions'].clear(),
    lambda b, a, o: o['mutations'][0].update(affected_rows=0),
    lambda b, a, o: o['mutations'][0].update(write_started_utc=b['observed_utc']),
    lambda b, a, o: a.update(initial_copy_job_evidence={'old_jobs_inventory_complete': False}),
])
def test_rotation_rejects_incomplete_or_altered_evidence(change):
    before, after, operations = fixture()
    change(before, after, operations)
    # Ces cas testent le contrat métier même avec un hash cohérent ;
    # les octets altérés sans repin sont testés séparément ci-dessous.
    for name in ('catalog_before','catalog_after'):
        operations[name]['output_sha256']=hashlib.sha256(operations[name]['output'].encode()).hexdigest()
    with pytest.raises(ValueError):
        p.evaluate_rotation(before, after, operations)


@pytest.mark.parametrize('change', [
    lambda c: c.update(output=c['output'].replace('ONLINE', 'SAVED')),
    lambda c: c.update(output_sha256='0'*64),
    lambda c: c.update(jar_sha256='d'*64),
    lambda c: c.update(jar_sha256=None),
    lambda c: c['reader'].update(uid='foreign'),
    lambda c: c['reader'].update(pod_uid='foreign'),
    lambda c: c['reader']['image_binding'].update(verified=False),
    lambda c: c['reader']['image_binding'].update(index='sha256:'+'d'*64),
    lambda c: c['reader']['image_binding'].update(observed_digest='sha256:'+'d'*64),
    lambda c: c['reader']['identity_effective'].update(verified=False),
])
def test_rotation_refuses_altered_catalogue_bytes_jar_or_reader(change):
    before, after, operations=fixture()
    change(operations['catalog_after'])
    with pytest.raises(ValueError):p.evaluate_rotation(before,after,operations)


@pytest.mark.parametrize('change', [
    lambda o: o.pop('journal_library'),
    lambda o: o.pop('journal_name'),
    lambda o: o['rotation'].pop('journal_library'),
    lambda o: o['rotation'].update(journal_name='FOREIGN'),
    lambda o: [o[k].update(journal_library='FOREIGN',journal_name='OTHER') for k in ('catalog_before','catalog_after')],
    lambda o: [o[k].update(journal_library='FOREIGN',journal_name='OTHER') for k in ('rotation','catalog_before','catalog_after')],
])
def test_rotation_requires_expected_journal_pins_and_actual_ack_scope(change):
    before,after,operations=fixture()
    change(operations)
    with pytest.raises(ValueError):p.evaluate_rotation(before,after,operations)


@pytest.mark.parametrize('rows',[[],[{'ID':999}]])
def test_rotation_evaluator_rejects_agreeing_but_wrong_business_state(rows):
    before,after,operations=fixture();after['source']=deepcopy(rows);after['mirror']=deepcopy(rows)
    with pytest.raises(ValueError):p.evaluate_rotation(before,after,operations)
