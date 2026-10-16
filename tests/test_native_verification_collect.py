"""Local processes and real stored/destination exporters, without native access."""
from dataclasses import replace
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
import stat
import sys

import pytest

from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry
from quadringent.qualification import verification_collect as collect
from quadringent.qualification.verification_contract import ReceiverWindow
from quadringent.qualification.verification_stream import FrameReader, StreamLimits, write_stream
from quadringent.qualification.verification_stored import ObjectReference, StoredReadSet
from test_native_verification_destination import fixture as destination_fixture
import test_native_verification_stored as stored_fixtures
from test_native_verification_stored import ROOT, STAMP, export, publish_batch, record

published = stored_fixtures.published


def process(scope, dataset, rows, *, suffix='', payload=None):
    output = BytesIO()
    write_stream(output, iter(rows), scope_sha256=scope.sha256, dataset=dataset,
                 started_at=STAMP, completed_at=STAMP)
    script = ("import os,sys; assert 'UNRELATED_TEST_SECRET' not in os.environ; "
              "assert sys.stdin.buffer.read()==b'fixture-stdin-secret'; "
              f"sys.stdout.buffer.write({(output.getvalue() if payload is None else payload)!r}); " + suffix)
    return collect.ProcessSpec((sys.executable, '-I', '-c', script), b'fixture-stdin-secret', {})


@pytest.fixture
def inputs(published):
    return make_inputs(published)


def make_inputs(published):
    store, scope, initial, intent, read_set, *_ = published
    snapshot, _ = export(published, dataset='snapshot')
    raw, _ = export(published)
    final = {}
    for row in raw:
        image = {column.name: row[column.name] for column in scope.columns}
        if row['OPERATION'] == 'd':
            final.pop(str(row['ID']), None)
        elif row['OPERATION'] != 'u_before':
            final[str(row['ID'])] = image
    data = {'source': list(final.values()), 'snapshot': snapshot,
            'journal': raw[len(snapshot):], 'raw': raw, 'history': raw,
            'mirror': list(final.values())}
    data['positions'] = [{key: row[key] for key in ('JOURNAL_RECEIVER', 'JOURNAL_SEQUENCE')}
                         for row in data['journal']]
    connections = []

    def connection_factory():
        dataset = 'history' if not connections else 'mirror'
        names = [column.name for column in scope.columns]
        if dataset == 'history':
            names += ['EVENT_ID', 'OPERATION', 'JOURNAL_RECEIVER', 'JOURNAL_SEQUENCE']
        rows = [tuple(Decimal(str(row[name])) if row[name] is not None and name in
                      {'ID', 'AMOUNT', 'JOURNAL_SEQUENCE'} else row[name] for name in names)
                for row in data[dataset]]
        _, connection = destination_fixture(dataset=dataset, custom_scope=scope, rows=rows)
        connection.closed = False

        def close():
            connection.closed = True
        connection.close = close
        connections.append(connection)
        return connection

    def windows(dataset):
        return tuple(collect.WindowProcess(window, process(scope, dataset,
                     [row for row in data[dataset] if row['JOURNAL_RECEIVER'] == window.identity.receiver_name]))
                     for window in scope.receiver_windows)
    return dict(scope=scope, source=process(scope, 'source', data['source']),
                journal=windows('journal'), positions=windows('positions'), store=store,
                raw_prefix_root=ROOT, initial_copy=initial, copy_intent=intent,
                raw_read_set=read_set, connection_factory=connection_factory,
                check_current=lambda: True), connections, data


def run(tmp_path, inputs, **changes):
    kwargs, *_ = inputs
    return collect.collect_verification(tmp_path / 'collection', **{**kwargs, **changes})


def test_real_producers_archive_all_seven_streams_and_objects(inputs, tmp_path, monkeypatch):
    monkeypatch.setenv('UNRELATED_TEST_SECRET', 'never-forward')
    result = run(tmp_path, inputs)
    assert result.report['state'] == 'verified'
    assert result.report['transaction_consistency_verified'] is False
    assert result.report['ignored_fields'] == 0
    assert set(result.streams) == {'source', 'snapshot', 'journal', 'positions', 'raw', 'history', 'mirror'}
    assert all(connection.closed for connection in inputs[1])
    assert len(inputs[1]) == 2
    for dataset, reference in result.streams.items():
        with reference.path.open('rb') as stream:
            reader = FrameReader(stream, scope_sha256=inputs[0]['scope'].sha256, dataset=dataset)
            assert list(reader)
            assert reader.summary == reference.summary
        assert reference.summary.sha256 == hashlib.sha256(reference.path.read_bytes()).hexdigest()
    assert any(original.object_key == inputs[0]['initial_copy'].key for original in result.originals)
    assert any(original.window == inputs[0]['scope'].receiver_windows[0] for original in result.originals)
    for original in result.originals:
        if original.dataset in {'source', 'journal', 'positions'}:
            assert original.process_return_code == 0
            assert original.stream_summary.sha256 == original.sha256
    with pytest.raises(TypeError):
        result.streams['source'] = result.streams['mirror']
    with pytest.raises(AttributeError):
        result.originals[0].sha256 = 'a' * 64
    for reference in (*result.originals, result.report_reference):
        assert hashlib.sha256(reference.path.read_bytes()).hexdigest() == reference.sha256
        assert stat.S_IMODE(reference.path.stat().st_mode) == 0o600
    for directory, _, files in os.walk(tmp_path / 'collection'):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert all(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode) == 0o600 for name in files)
    assert json.loads(result.report_reference.path.read_bytes()) == result.report
    assert 'fixture-stdin-secret' not in repr(result)
    assert 'fixture-stdin-secret' not in repr(inputs[0]['source'])


def test_two_receiver_originals_are_preserved_and_merged_in_scope_order(published, tmp_path):
    store, scope, initial, intent, inventory, first, second = published
    identity = ReceiverIdentity('TESTLIB', 'JRN', 'TESTLIB', 'RCV2', 'offline-v1', 'generation-two')
    end = scope.capture_binding.checkpoint(identity, 1)
    registry = ReceiverNameRegistry(FileObjectStore(store.root / 'receiver-authority-v1'),
                                   authority=scope.capture_binding.registry_authority, source_system='ibmi', journal='JRN')
    registry.claim(scope.capture_binding, end)
    scope = replace(scope, checkpoint=end,
                    receiver_windows=scope.receiver_windows + (ReceiverWindow(identity, 1, 1),))
    third = {'ID': 3, 'LABEL': 'third', 'AMOUNT': '3.00'}
    event = replace(record(scope, 1, third), position=JournalPosition('RCV2', 1))
    batch = publish_batch(tmp_path, store, scope, [event])
    inventory = StoredReadSet(scope.sha256, inventory.batches + (batch,))
    bundle = make_inputs((store, scope, initial, intent, inventory, first, second))
    result = run(tmp_path, bundle)
    assert result.report['state'] == 'verified'
    for dataset in ('journal', 'positions'):
        original_windows = [reference.window for reference in result.originals if reference.dataset == dataset]
        assert original_windows == list(scope.receiver_windows)
        with result.streams[dataset].path.open('rb') as stream:
            rows = list(FrameReader(stream, scope_sha256=scope.sha256, dataset=dataset))
        assert [row['JOURNAL_RECEIVER'] for row in rows] == ['RCV1', 'RCV2']
        assert result.streams[dataset].path.read_bytes().count(b'"kind":"header"') == 1
        assert result.streams[dataset].path.read_bytes().count(b'"kind":"end"') == 1
    with pytest.raises(collect.CollectionError):
        collect.collect_verification(tmp_path / 'wrong-order', **{**bundle[0], 'journal': bundle[0]['journal'][::-1]})
    assert not (tmp_path / 'wrong-order').exists()


@pytest.mark.parametrize('fault', ['exit', 'eof', 'scope', 'position'])
def test_failed_process_never_leaves_an_accepted_collection(inputs, tmp_path, fault):
    kwargs = inputs[0]
    if fault in {'exit', 'eof'}:
        bad = process(kwargs['scope'], 'source', inputs[2]['source'],
                      suffix="sys.stderr.write('SYNTHETIC_SECRET'); sys.exit(7)" if fault == 'exit' else '',
                      payload=b'no eof' if fault == 'eof' else None)
        changes = {'source': bad}
    else:
        rows = [dict(inputs[2]['positions'][0], JOURNAL_SEQUENCE='999')] if fault == 'position' else inputs[2]['positions']
        scope = replace(kwargs['scope'], producer_revision='c' * 40) if fault == 'scope' else kwargs['scope']
        changes = {'positions': (collect.WindowProcess(kwargs['scope'].receiver_windows[0], process(scope, 'positions', rows)),)}
    with pytest.raises(collect.CollectionError) as error:
        run(tmp_path, inputs, **changes)
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    assert not (tmp_path / 'collection').exists()


def test_missing_stored_payload_refuses_and_removes_the_whole_collection(inputs, tmp_path):
    kwargs = inputs[0]
    kwargs['store']._path_for(kwargs['raw_read_set'].batches[-1].payload.key).unlink()
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs)
    assert not (tmp_path / 'collection').exists()


@pytest.mark.parametrize('when', ['initial', 'after_source', 'after_all'])
def test_superseded_configuration_cannot_return_a_report(inputs, tmp_path, when):
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        if when == 'initial':
            return False
        if when == 'after_source':
            return not (tmp_path / 'collection' / 'source' / 'original.ndjson').exists()
        return len(inputs[1]) < 2
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs, check_current=current)
    assert calls
    assert not (tmp_path / 'collection').exists()


def test_mutated_original_is_refused_before_ledger(inputs, tmp_path, monkeypatch):
    factory = inputs[0]['connection_factory']

    def connection_factory():
        connection = factory()
        close = connection.close

        def changed():
            close()
            if len(inputs[1]) == 2:
                original = tmp_path / 'collection' / 'source' / 'original.ndjson'
                original.write_bytes(original.read_bytes().replace(b'Caf', b'Bad'))
        connection.close = changed
        return connection
    monkeypatch.setattr(collect, 'DiskReconciler', lambda *_a, **_kw: pytest.fail('ledger must not consume a changed archive'))
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs, connection_factory=connection_factory)
    assert not (tmp_path / 'collection').exists()


@pytest.mark.parametrize('fault', ['fetch', 'close'])
def test_destination_errors_close_connection_and_prevent_report(inputs, tmp_path, fault):
    factory = inputs[0]['connection_factory']

    def failing_factory():
        connection = factory()
        if fault == 'fetch':
            connection.cursors[1].fault = 'fetch'
        else:
            def close():
                connection.closed = True
                raise OSError('SYNTHETIC_SECRET')
            connection.close = close
        return connection
    with pytest.raises(collect.CollectionError) as error:
        run(tmp_path, inputs, connection_factory=failing_factory)
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    assert all(connection.closed for connection in inputs[1])
    assert not (tmp_path / 'collection').exists()


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'foreign', 'untrusted_guard'])
def test_all_window_specs_and_guard_are_checked_before_collection(inputs, tmp_path, change):
    windows = inputs[0]['positions']
    if change == 'missing':
        changes = {'positions': ()}
    elif change == 'duplicate':
        changes = {'positions': windows * 2}
    elif change == 'foreign':
        changes = {'positions': (replace(windows[0], window=replace(windows[0].window, end_sequence=22)),)}
    else:
        changes = {'check_current': lambda: {'verified': True}}
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs, **changes)
    assert not inputs[1]
    assert not (tmp_path / 'collection').exists()


def test_existing_output_is_untouched(inputs, tmp_path):
    directory = tmp_path / 'collection'
    directory.mkdir()
    sentinel = directory / 'keep'
    sentinel.write_bytes(b'unchanged')
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs)
    assert sentinel.read_bytes() == b'unchanged'


def test_budget_failure_is_not_partial_success(inputs, tmp_path):
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs, limits=StreamLimits(max_rows=1))
    assert not (tmp_path / 'collection').exists()


def test_process_spec_owns_a_hidden_immutable_environment():
    environment = {'TOKEN': 'SYNTHETIC_SECRET'}
    spec = collect.ProcessSpec(('python',), b'SYNTHETIC_SECRET', environment)
    environment['TOKEN'] = 'changed'
    assert spec.environment['TOKEN'] == 'SYNTHETIC_SECRET'
    assert 'SYNTHETIC_SECRET' not in repr(spec)
    with pytest.raises(TypeError):
        spec.environment['TOKEN'] = 'changed'


@pytest.mark.parametrize('late', [False, True])
def test_real_collection_preserves_row_image_chain_refusal(published, tmp_path, late):
    store, scope, initial, intent, _, first, _ = published
    identity = scope.copy_boundary.receiver_identity
    scope = replace(scope, checkpoint=scope.capture_binding.checkpoint(identity, 22),
                    receiver_windows=(ReceiverWindow(identity, 21, 22),))
    before = {**first, 'LABEL': 'A'}
    after = {**first, 'LABEL': 'B'}
    snapshot = publish_batch(tmp_path, store, scope,
                             [record(scope, 1, after if late else before, snapshot=True)], snapshot=True)
    journal = publish_batch(tmp_path, store, scope,
                            [record(scope, 21, before, operation='u_before'),
                             record(scope, 22, after, operation='u_after')])
    evidence = json.loads(store._path_for(initial.key).read_bytes())
    evidence['snapshot_batches'] = [{'payload_key': snapshot.payload.key.rsplit('/', 1)[1],
                                     'manifest_key': snapshot.manifest.key.rsplit('/', 1)[1]}]
    content = json.dumps(evidence).encode()
    store._path_for(initial.key).write_bytes(content)
    initial = ObjectReference(initial.key, hashlib.sha256(content).hexdigest())
    inventory = StoredReadSet(scope.sha256, (snapshot, journal))
    bundle = make_inputs((store, scope, initial, intent, inventory, before, after))
    result = run(tmp_path, bundle)
    assert all(comparison['equal'] for comparison in result.report['comparisons'].values())
    assert result.report['checks']['journal_row_images_reconstructed'] is (not late)
    assert result.report['state'] == ('divergent' if late else 'verified')
    assert result.report['transaction_consistency_verified'] is False


def test_superseded_during_ledger_is_rechecked_before_return(inputs, tmp_path, monkeypatch):
    current = True
    original = collect.DiskReconciler

    class ChangedLedger(original):
        def finish(self):
            nonlocal current
            result = super().finish()
            assert result['state'] == 'verified'
            current = False
            return result
    monkeypatch.setattr(collect, 'DiskReconciler', ChangedLedger)
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs, check_current=lambda: current)
    assert not (tmp_path / 'collection').exists()


def test_durable_write_failure_cannot_return_or_retain_a_collection(inputs, tmp_path, monkeypatch):
    original = collect.os.fsync
    calls = 0

    def fail(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError('SYNTHETIC_SECRET')
        return original(descriptor)
    monkeypatch.setattr(collect.os, 'fsync', fail)
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs)
    assert not (tmp_path / 'collection').exists()


def test_joint_window_row_budget_prevents_a_partial_chain(published, tmp_path):
    store, scope, initial, intent, inventory, first, second = published
    identity = ReceiverIdentity('TESTLIB', 'JRN', 'TESTLIB', 'RCV2', 'offline-v1', 'generation-two')
    end = scope.capture_binding.checkpoint(identity, 2)
    registry = ReceiverNameRegistry(FileObjectStore(store.root / 'receiver-authority-v1'),
                                   authority=scope.capture_binding.registry_authority, source_system='ibmi', journal='JRN')
    registry.claim(scope.capture_binding, end)
    scope = replace(scope, checkpoint=end,
                    receiver_windows=scope.receiver_windows + (ReceiverWindow(identity, 1, 2),))
    before = replace(record(scope, 1, second, operation='u_before'), position=JournalPosition('RCV2', 1))
    after = replace(record(scope, 2, {**second, 'LABEL': 'updated'}, operation='u_after'),
                    position=JournalPosition('RCV2', 2))
    batch = publish_batch(tmp_path, store, scope, [before, after])
    inventory = StoredReadSet(scope.sha256, inventory.batches + (batch,))
    bundle = make_inputs((store, scope, initial, intent, inventory, first, second))
    assert len(bundle[2]['source']) == 2 and len(bundle[2]['journal']) == 3
    with pytest.raises(collect.CollectionError):
        run(tmp_path, bundle, limits=StreamLimits(max_rows=2))
    assert not (tmp_path / 'collection').exists()


def test_persistent_collection_inventory_survives_the_python_return(inputs, tmp_path):
    result = run(tmp_path, inputs)
    reference = result.collection_reference
    content = reference.path.read_bytes()
    assert reference.path.name == 'collection.json'
    assert reference.sha256 == hashlib.sha256(content).hexdigest()
    assert reference.byte_count == len(content)
    inventory = json.loads(content)
    assert inventory['format_version'] == 'quadringent-native-table-collection-v1'
    assert inventory['scope_sha256'] == inputs[0]['scope'].sha256
    assert inventory['native_admission'] == 'not_evaluated'
    assert inventory['native_cdc_qualified'] is False
    assert inventory['context_enforcement'] == 'caller_configuration_fence_only'
    assert set(inventory['streams']) == set(result.streams)
    assert len(inventory['originals']) == len(result.originals)
    for item in [inventory['comparison'], *inventory['streams'].values(), *inventory['originals']]:
        path = reference.path.parent / item['path']
        assert not item['path'].startswith('/') and '..' not in item['path'].split('/')
        assert path.stat().st_size == item['byte_count']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    for dataset, item in inventory['streams'].items():
        with (reference.path.parent / item['path']).open('rb') as original:
            reader = FrameReader(original, scope_sha256=inventory['scope_sha256'], dataset=dataset)
            list(reader)
            assert reader.summary.to_dict() == item['summary']
    assert inventory['comparison']['sha256'] == result.report_reference.sha256
    assert 'fixture-stdin-secret' not in content.decode()
    assert 'UNRELATED_TEST_SECRET' not in content.decode()
    assert 'command' not in inventory and 'environment' not in inventory


def test_collection_inventory_fsync_failure_removes_comparison_and_archives(inputs, tmp_path, monkeypatch):
    fsync = collect.os.fsync
    target = tmp_path / 'collection' / 'collection.json'

    def fail(descriptor):
        if target.exists() and os.fstat(descriptor).st_ino == target.stat().st_ino:
            raise OSError('SYNTHETIC_SECRET')
        return fsync(descriptor)
    monkeypatch.setattr(collect.os, 'fsync', fail)
    with pytest.raises(collect.CollectionError):
        run(tmp_path, inputs)
    assert not (tmp_path / 'collection').exists()
