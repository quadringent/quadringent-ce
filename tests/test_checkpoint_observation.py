"""One bounded read supplies each checkpoint and its retained provenance."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from unittest.mock import patch

import pytest

from quadringent.checkpoint import DynamoDbCheckpointStore, JsonCheckpointStore
from quadringent.gcs_backend import GcsCheckpointStore
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from test_gcs_backend import FakeGcsClient, GcsError


BOUND = BoundCheckpoint('scope-a', 'epoch-a', ReceiverIdentity(
    'JLIB', 'JRN', 'RLIB', 'RCV1', 'native-test-v1', 'opaque-generation-a'), 20)
MAX_BYTES = 65536


def encoded(position=BOUND, *, stream=False):
    value = position.to_dict()
    if stream:
        value['stream_id'] = 'stream-a'
    return json.dumps(value, indent=2, ensure_ascii=True).encode() + b'\n  '


def digest(content):
    return 'sha256:' + hashlib.sha256(content).hexdigest()


def dynamo_item(position=BOUND, partition='pk'):
    payload = json.dumps(position.to_dict(), sort_keys=True, separators=(',', ':')).encode()
    return {partition: {'S': 'stream-a'}, 'format_version': {'S': position.FORMAT_VERSION},
            'scope_id': {'S': position.scope_id}, 'capture_epoch': {'S': position.capture_epoch},
            'receiver_identity': {'M': {key: {'S': value}
                                     for key, value in position.receiver_identity.to_dict().items()}},
            'receiver': {'S': position.position.receiver}, 'sequence': {'N': str(position.sequence)},
            'checkpoint_digest': {'S': hashlib.sha256(payload).hexdigest()}}


class Dynamo:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) != 1:
            raise AssertionError('second checkpoint read')
        return {} if self.item is None else {'Item': deepcopy(self.item)}

    def put_item(self, **kwargs):
        raise AssertionError('observation must never write')


class Gcs:
    def __init__(self, content, generation=7, fault=None):
        self.content, self.generation, self.fault = content, generation, fault
        self.reads, self.metadata_reads = [], []
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return self

    def get_blob(self, name):
        self.metadata_reads.append(name)
        return None if self.content is None else self

    def download_as_bytes(self, **kwargs):
        self.reads.append(kwargs)
        if len(self.reads) != 1:
            raise AssertionError('second content read')
        if self.fault:
            raise GcsError(self.fault)
        return self.content[:kwargs.get('end', len(self.content)) + 1]

    def upload_from_string(self, *args, **kwargs):
        raise AssertionError('observation must never write')


@pytest.mark.parametrize('backend', ['json', 'gcs', 'dynamo'])
def test_absence_is_explicit_without_writes(backend, tmp_path):
    if backend == 'json':
        store = JsonCheckpointStore(tmp_path / 'absent')
    elif backend == 'gcs':
        store = GcsCheckpointStore('sandbox-state', 'stream-a', client=Gcs(None))
    else:
        store = DynamoDbCheckpointStore('checkpoints', 'stream-a', client=Dynamo(None))
    assert store.observe_bound() is None
    assert list(tmp_path.iterdir()) == []


def test_json_preserves_exact_bytes_and_never_uses_load_bound(tmp_path):
    content = encoded()
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(content)
    with patch.object(JsonCheckpointStore, 'load_bound', side_effect=AssertionError('second read')):
        observed = JsonCheckpointStore(path).observe_bound()
    assert observed.position == BOUND and observed.content == content
    assert observed.encoding == 'checkpoint-json-v2' and observed.version == digest(content)
    with pytest.raises(FrozenInstanceError):
        observed.version = 'invented'
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('bad', [b'{}', b'null', b'[]', b'', b'{"sequence":1,"sequence":2}',
    b'{"format_version":"as400-checkpoint-v1","receiver":"RCV1","sequence":20}',
    b'{"receiver":"RCV1","sequence":20}', b'\xff', encoded().decode().encode('utf-16')])
def test_json_refuses_legacy_corruption_or_ambiguous_parse(tmp_path, bad):
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(bad)
    with pytest.raises(ValueError):
        JsonCheckpointStore(path).observe_bound()


def test_json_refuses_oversized_content_before_parse(tmp_path):
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(encoded() + b' ' * MAX_BYTES)
    with patch('quadringent.checkpoint._decode_checkpoint_json', side_effect=AssertionError('unbounded parse')):
        with pytest.raises(ValueError):
            JsonCheckpointStore(path).observe_bound()


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo', 'parent_symlink'])
def test_json_refuses_symlink_and_nonregular_files(tmp_path, kind):
    real = tmp_path / 'real'
    real.write_bytes(encoded())
    target = tmp_path / 'checkpoint.json'
    if kind == 'symlink':
        target.symlink_to(real)
    elif kind == 'directory':
        target.mkdir()
    elif kind == 'fifo':
        os.mkfifo(target)
    else:
        directory = tmp_path / 'original'
        directory.mkdir()
        (directory / 'checkpoint.json').write_bytes(encoded())
        (tmp_path / 'linked').symlink_to(directory, target_is_directory=True)
        target = tmp_path / 'linked' / 'checkpoint.json'
    with pytest.raises(ValueError):
        JsonCheckpointStore(target).observe_bound()


@pytest.mark.parametrize('mutation', ['replace', 'rewrite'])
def test_json_refuses_changes_during_the_single_observation(tmp_path, monkeypatch, mutation):
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(encoded())
    original = os.read
    changed = False

    def read(descriptor, size):
        nonlocal changed
        result = original(descriptor, size)
        if not changed:
            changed = True
            replacement = tmp_path / 'replacement'
            replacement.write_bytes(encoded(BoundCheckpoint(BOUND.scope_id, BOUND.capture_epoch,
                                                            BOUND.receiver_identity, 21)))
            if mutation == 'replace':
                replacement.replace(path)
            else:
                path.write_bytes(replacement.read_bytes())
                replacement.unlink()
        return result

    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(ValueError):
        JsonCheckpointStore(path).observe_bound()


@pytest.mark.parametrize('partition', ['pk', 'stream_id'])
def test_dynamo_observes_the_single_strong_read_typed_item(partition):
    item = dynamo_item(partition=partition)
    client = Dynamo(item)
    store = DynamoDbCheckpointStore('checkpoints', 'stream-a', client=client, partition_key=partition)
    with patch.object(store, 'load_bound', side_effect=AssertionError('second read')):
        observed = store.observe_bound()
    assert observed.position == BOUND
    assert observed.content == json.dumps(item, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True, allow_nan=False).encode()
    assert observed.encoding == 'dynamodb-attribute-item-v1'
    assert observed.version == digest(observed.content)
    assert client.calls == [{'TableName': 'checkpoints', 'Key': {partition: {'S': 'stream-a'}},
                             'ConsistentRead': True}]


@pytest.mark.parametrize('mutation', ['digest', 'receiver', 'partition', 'identity', 'legacy', 'number', 'extra', 'overflow'])
def test_dynamo_rejects_inconsistent_identity_digest_and_typed_attributes(mutation):
    item = dynamo_item()
    if mutation == 'digest':
        item['checkpoint_digest']['S'] = '0' * 64
    elif mutation == 'receiver':
        item['receiver']['S'] = 'OTHER'
    elif mutation == 'partition':
        item['pk']['S'] = 'another-stream'
    elif mutation == 'identity':
        item['receiver_identity']['M']['native_generation']['S'] = 'replacement'
    elif mutation == 'legacy':
        item = {'pk': {'S': 'stream-a'}, 'receiver': {'S': 'RCV1'}, 'sequence': {'N': '20'}}
    elif mutation == 'number':
        item['sequence'] = {'N': '020'}
    elif mutation == 'extra':
        item['scope_id']['ignored'] = 'value'
    else:
        item['receiver_identity']['M']['native_generation']['S'] = 'a' * MAX_BYTES
    client = Dynamo(item)
    with pytest.raises(ValueError):
        DynamoDbCheckpointStore('checkpoints', 'stream-a', client=client).observe_bound()
    assert len(client.calls) == 1


@pytest.mark.parametrize('generation', [7, '7', None])
def test_gcs_retains_the_bytes_and_the_generation_used_for_that_download(generation):
    content = encoded(stream=True)
    client = Gcs(content, generation)
    store = GcsCheckpointStore('sandbox-state', 'stream-a', client=client)
    with patch.object(store, 'load_bound', side_effect=AssertionError('second read')):
        observed = store.observe_bound()
    assert observed.position == BOUND and observed.content == content
    assert observed.encoding == 'checkpoint-json-v2'
    assert observed.version == (digest(content) if generation is None else 'gcs-generation:7')
    assert client.metadata_reads == [store.object_name]
    assert len(client.reads) == 1
    assert client.reads[0]['start'] == 0 and client.reads[0]['end'] == MAX_BYTES
    if generation is not None:
        assert client.reads[0]['if_generation_match'] == 7


@pytest.mark.parametrize('code', [404, 412])
def test_gcs_generation_change_after_metadata_is_refused_without_reread(code):
    client = Gcs(encoded(stream=True), fault=code)
    with pytest.raises(ValueError):
        GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    assert len(client.reads) == len(client.metadata_reads) == 1


@pytest.mark.parametrize('mutation', ['overflow', 'legacy', 'stream', 'duplicate', 'generation'])
def test_gcs_refuses_overflow_legacy_wrong_stream_or_ambiguous_provenance(mutation):
    content = encoded(stream=True)
    if mutation == 'overflow':
        content += b' ' * MAX_BYTES
    elif mutation == 'legacy':
        content = b'{"stream_id":"stream-a","receiver":"RCV1","sequence":20,"format_version":"as400-checkpoint-v1"}'
    elif mutation == 'stream':
        content = content.replace(b'stream-a', b'stream-b')
    elif mutation == 'duplicate':
        content = content.replace(b'"sequence": 20', b'"sequence": 20, "sequence": 21')
    client = Gcs(content, True if mutation == 'generation' else 7)
    with pytest.raises(ValueError):
        GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()


def test_gcs_works_with_the_existing_generation_cas_fake_without_writes():
    client = FakeGcsClient()
    store = GcsCheckpointStore('sandbox-state', 'stream-a', client=client)
    client.objects[('sandbox-state', store.object_name)] = (encoded(stream=True), {}, 9)
    observed = store.observe_bound()
    assert observed.version == 'gcs-generation:9' and observed.position == BOUND
    assert client.writes == []


def test_observation_rejects_position_unrelated_to_the_retained_content():
    from quadringent.checkpoint import CheckpointObservation

    other = BoundCheckpoint(BOUND.scope_id, BOUND.capture_epoch, BOUND.receiver_identity, 21)
    content = encoded()
    with pytest.raises(ValueError):
        CheckpointObservation(other, content, 'checkpoint-json-v2', digest(content))


@pytest.mark.parametrize('encoding,version', [([], 'sha256:invalid'), ('unknown', 'sha256:invalid'),
    ('checkpoint-json-v2', 'sha256:' + '0' * 64), ('dynamodb-attribute-item-v1', 'gcs-generation:7')])
def test_observation_rejects_ambiguous_encoding_or_version(encoding, version):
    from quadringent.checkpoint import CheckpointObservation

    with pytest.raises(ValueError):
        CheckpointObservation(BOUND, encoded(), encoding, version)


@pytest.mark.parametrize('backend', ['json', 'gcs', 'dynamo'])
def test_observation_validates_its_stream_without_another_backend_read(backend, tmp_path):
    if backend == 'json':
        path = tmp_path / 'checkpoint.json'
        path.write_bytes(encoded())
        observed = JsonCheckpointStore(path).observe_bound()
        assert observed.require_stream('stream-a') == BOUND
        # The local path supplies scope; a local envelope contains no stream-id attribute.
        return
    if backend == 'gcs':
        client = Gcs(encoded(stream=True))
        observed = GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    else:
        client = Dynamo(dynamo_item())
        observed = DynamoDbCheckpointStore('checkpoints', 'stream-a', client=client).observe_bound()
    assert observed.require_stream('stream-a') == BOUND
    with pytest.raises(ValueError):
        observed.require_stream('stream-b')
    assert len(client.reads if backend == 'gcs' else client.calls) == 1


def test_missing_gcs_generation_attribute_uses_only_content_fingerprint():
    client = Gcs(encoded(stream=True))
    del client.generation
    observed = GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    assert observed.version == digest(client.content)


def test_gcs_does_not_label_new_generation_discovered_after_an_unpinned_read():
    client = Gcs(encoded(stream=True), None)
    original = client.download_as_bytes

    def download(**kwargs):
        result = original(**kwargs)
        client.generation = 8
        return result

    client.download_as_bytes = download
    observed = GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    assert observed.version == digest(client.content)


def test_exact_byte_budget_is_accepted_without_truncating_originals(tmp_path):
    path = tmp_path / 'checkpoint.json'
    content = encoded()
    content += b' ' * (MAX_BYTES - len(content))
    path.write_bytes(content)
    assert JsonCheckpointStore(path).observe_bound().content == content


def test_dynamo_enforces_the_budget_before_projecting_the_typed_item():
    item = dynamo_item()
    item['receiver_identity']['M']['native_generation']['S'] = 'a' * MAX_BYTES
    client = Dynamo(item)
    with patch('quadringent.checkpoint._observed_dynamo', side_effect=AssertionError('unbounded parse')):
        with pytest.raises(ValueError):
            DynamoDbCheckpointStore('checkpoints', 'stream-a', client=client).observe_bound()
    assert len(client.calls) == 1


def test_gcs_enforces_the_budget_before_json_parsing():
    client = Gcs(encoded(stream=True) + b' ' * MAX_BYTES)
    with patch('quadringent.checkpoint._decode_checkpoint_json', side_effect=AssertionError('unbounded parse')):
        with pytest.raises(ValueError):
            GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    assert len(client.reads) == 1


def test_gcs_refuses_generation_metadata_changed_by_the_download():
    client = Gcs(encoded(stream=True))
    original = client.download_as_bytes

    def download(**kwargs):
        result = original(**kwargs)
        client.generation = 8
        return result

    client.download_as_bytes = download
    with pytest.raises(ValueError):
        GcsCheckpointStore('sandbox-state', 'stream-a', client=client).observe_bound()
    assert len(client.reads) == 1
