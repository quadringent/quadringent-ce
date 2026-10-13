"""In-memory durable capture state; no cloud clients or source authentication."""
import json
from pathlib import Path
from types import SimpleNamespace

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry


def installed_capture_state(root, environ):
    """Persist a synthetic original admission through the production stores."""
    root = Path(root)
    environment = dict(environ)
    authority = f"s3://{environment['AS400_RAW_BUCKET']}/receiver-authority-v1"
    identity = ReceiverIdentity(environment['AS400_JOURNAL_LIBRARY'],
                                environment['AS400_JOURNAL_NAME'], 'RCVLIB', 'RCV1',
                                'offline-test-v1', 'original-generation')
    authority_store = FileObjectStore(root / 'authority')
    registry = ReceiverNameRegistry(authority_store, authority=authority,
                                    source_system='ibmi', journal=identity.journal_name)
    binding = registry.create_or_load_binding(scope_id=environment['AS400_STREAM_KEY'],
                                               identity=identity, sequence=20)
    checkpoint = JsonCheckpointStore(root / 'checkpoint.json')
    checkpoint.compare_and_set_bound(None, binding.bootstrap)
    environment['AS400_CAPTURE_BINDING_JSON'] = json.dumps(binding.to_dict())
    return SimpleNamespace(environment=environment, binding=binding, registry=registry,
                           checkpoint=checkpoint, authority_store=authority_store)


class MemoryCaptureStore:
    def __init__(self, storage, prefix):
        self.storage = storage
        self.prefix = prefix.strip("/")

    def _key(self, key):
        return self.prefix + "/" + key

    def get_bounded(self, key, max_bytes):
        if self._key(key) not in self.storage.objects:
            raise FileNotFoundError(key)
        content = self.storage.objects[self._key(key)]
        if len(content) > max_bytes:
            raise ValueError("bounded object read exceeded")
        return content

    def put_once(self, key, body):
        name = self._key(key)
        previous = self.storage.objects.get(name)
        if previous is not None and previous != body:
            raise ValueError("immutable artifact collision")
        self.storage.objects[name] = body

    def list_receipt_keys(self, limit):
        return tuple(self.storage.receipts[:limit])


class MemoryCaptureCheckpoint:
    def __init__(self):
        self.record = None

    def load_bound(self):
        if isinstance(self.record, JournalPosition):
            raise ValueError("legacy checkpoint v1 cannot be bound")
        return self.record


class MemoryCaptureStorage:
    def __init__(self):
        self.objects = {}
        self.checkpoints = {}
        self.receipts = []

    def object_store(self, prefix):
        return MemoryCaptureStore(self, prefix)

    def checkpoint_store(self, stream_key):
        return self.checkpoints.setdefault(stream_key, MemoryCaptureCheckpoint())
