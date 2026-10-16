"""Operator-owned collection plans; no native authority is inferred from a plan."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

from .verification_collect import ProcessSpec, WindowProcess, collect_verification
from .verification_contract import VerificationScope
from .verification_guard import _collect_native_receipts
from .verification_native import load_native_copy_inputs, native_journal_processes
from .verification_process import collect_process_document
from .verification_stored import BatchReference, ObjectReference, StoredReadSet


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError('invalid_collection_plan')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_plan_field')
        result[key] = value
    return result


def _absolute(value):
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError('invalid_plan_path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_plan_path')
    return path


def _read(path, maximum, *, private=True):
    path = _absolute(str(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum
                or private and (stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.getuid())):
            raise ValueError('invalid_private_input')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if not data or len(data) > maximum or identity(before) != identity(after) or identity(after) != identity(path.stat()):
            raise ValueError('changed_private_input')
        return data, identity(after)
    finally:
        os.close(descriptor)


class _Inputs:
    def __init__(self):
        self.pins = {}

    def read(self, path, maximum):
        path = _absolute(path)
        data, identity = _read(path, maximum)
        pin = (maximum, identity, hashlib.sha256(data).digest())
        if path in self.pins and self.pins[path] != pin:
            raise ValueError('changed_private_input')
        self.pins[path] = pin
        return data

    def current(self):
        try:
            for path, (maximum, identity, digest) in self.pins.items():
                data, observed = _read(path, maximum)
                if observed != identity or hashlib.sha256(data).digest() != digest:
                    return False
            return True
        except Exception:
            return False


class _LocalStore:
    def __init__(self, root):
        self.root = _absolute(root)
        if not self.root.is_dir():
            raise ValueError('store_unavailable')

    def get_bounded(self, key, maximum):
        ObjectReference(key, '0' * 64)
        return _read(self.root / key, maximum, private=False)[0]


def _store(config):
    if config.get('backend') == 'local':
        _fields(config, 'backend root')
        return _LocalStore(config['root'])
    _fields(config, 'backend bucket')
    if config['backend'] == 'aws':
        from quadringent.object_store import S3ObjectStore
        return S3ObjectStore(config['bucket'])
    if config['backend'] == 'gcs':
        from quadringent.gcs_backend import GcsObjectStore
        return GcsObjectStore(config['bucket'])
    raise ValueError('unsupported_store')


def _object(value):
    return ObjectReference(**_fields(value, 'key sha256'))


def _batch(value):
    _fields(value, 'manifest payload receipt')
    return BatchReference(_object(value['manifest']), _object(value['payload']),
                          _object(value['receipt']) if value['receipt'] is not None else None)


def _process(value, inputs):
    _fields(value, 'command input_path environment')
    if type(value['command']) is not list or type(value['environment']) is not dict:
        raise ValueError('invalid_process_plan')
    return ProcessSpec(tuple(value['command']), inputs.read(value['input_path'], 65536), value['environment'])


def _connect(destination, private_key):
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key, password=None)
    encoded = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return snowflake.connector.connect(
        account=destination['account'], user=destination['user'], role=destination['role'],
        warehouse=destination['warehouse'], private_key=encoded, login_timeout=15, network_timeout=30,
        session_parameters={'QUERY_TAG': 'QUADRINGENT_TABLE_COLLECTION', 'STATEMENT_TIMEOUT_IN_SECONDS': 900},
    )


def _save_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _native_journal(config, scope, store, initial_copy, out_dir, inputs, max_seconds):
    _fields(config, 'producer java classpath input_path environment')
    if config['producer'] != 'ibmi-fixed-copy-v1':
        raise ValueError('unsupported_native_journal_producer')

    class Authority:
        def get_bounded(self, key, maximum):
            return store.get_bounded('receiver-authority-v1/' + key, maximum)

    raw = store.get_bounded(initial_copy.key, 65536)
    copy = load_native_copy_inputs(Authority(), raw, expected_sha256=initial_copy.sha256,
                                  evidence_key=initial_copy.key, scope=scope)
    credentials = inputs.read(config['input_path'], 65536)
    directory = _absolute(str(out_dir) + '-native-inputs')
    directory.mkdir(mode=0o700)
    bundle = directory / 'descriptor.bundle'
    guard = directory / 'journal-guard.json'
    _save_private(bundle, copy.bundle)
    inputs.read(str(bundle), 393600)
    processes = native_journal_processes(
        copy, scope, java=config['java'], classpath=config['classpath'], credentials=credentials,
        environment=config['environment'], bundle_path=bundle, guard_receipt_path=guard,
        max_seconds=min(max_seconds, 900),
    )
    return processes, dict(
        format_version='quadringent-native-copy-collection-inputs-v1', scope_sha256=scope.sha256,
        context_sha256=copy.context_sha256, capture_binding_sha256=copy.binding_sha256,
        copy_boundary_sha256=copy.boundary_sha256, copy_run_id=scope.copy_run_id,
        initial_copy=dict(key=initial_copy.key, sha256=initial_copy.sha256),
        bundle_path=str(bundle), bundle_sha256=copy.bundle_sha256,
        guard_receipt_path=str(guard), native_admission='not_evaluated',
        _producer=processes[0].process,
    )


def _native_receipt(native, inputs, max_seconds):
    guard = _absolute(native['guard_receipt_path'])
    result = _collect_native_receipts(native, native['_producer'], guard_receipt_path=guard,
        directory=guard.parent/'after-all-streams', check_current=inputs.current,
        max_seconds=min(60, max_seconds), collect_document=collect_process_document)
    for reference in (result.journal_original, result.final_original):
        raw = inputs.read(str(reference.path), 4*1024**2)
        if len(raw) != reference.byte_count or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError('native_final_guard_differs')
    if inputs.current() is not True:
        raise ValueError('changed_native_input')
    # This preserves a producer measurement, not an independently admitted proof.
    native = {**{key: value for key, value in native.items() if key != '_producer'},
              **result.metadata}
    path = guard.parent / 'native-copy-inputs.json'
    _save_private(path, (json.dumps(native, sort_keys=True, separators=(',', ':')) + '\n').encode())
    return path


def run_collection(config, out_dir, *, stdout=None):
    stdout = sys.stdout if stdout is None else stdout
    try:
        started = time.monotonic()
        inputs = _Inputs()
        plan = json.loads(inputs.read(config, 1024 * 1024), object_pairs_hook=_pairs)
        _fields(plan, 'format_version scope storage raw_prefix_root initial_copy copy_intent raw_read_set '
                      'source journal positions destination max_seconds')
        if plan['format_version'] not in {'quadringent-collection-plan-v1', 'quadringent-collection-plan-v2'}:
            raise ValueError('unsupported_collection_plan')
        scope = VerificationScope.from_dict(plan['scope'])
        if type(plan['max_seconds']) is not int or not 1 <= plan['max_seconds'] <= 3600:
            raise ValueError('invalid_collection_budget')

        def remaining():
            seconds = math.floor(plan['max_seconds'] - (time.monotonic() - started))
            if seconds < 1:
                raise ValueError('collection_deadline')
            return seconds
        processes = {}
        for dataset in ('journal', 'positions'):
            if dataset == 'journal' and plan['format_version'] == 'quadringent-collection-plan-v2':
                continue
            values = plan[dataset]
            if type(values) is not list or len(values) != len(scope.receiver_windows):
                raise ValueError('invalid_process_windows')
            processes[dataset] = tuple(WindowProcess(window, _process(value, inputs))
                                       for window, value in zip(scope.receiver_windows, values, strict=True))
        source = _process(plan['source'], inputs)
        destination = _fields(plan['destination'], 'account user role warehouse private_key_path')
        for field in ('account', 'user', 'role', 'warehouse'):
            value = destination[field]
            if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_$.-]{1,255}', value) is None:
                raise ValueError('invalid_destination')
        private_key = inputs.read(destination['private_key_path'], 32768)
        if type(plan['raw_read_set']) is not list or len(plan['raw_read_set']) > 100_000:
            raise ValueError('invalid_read_set')
        read_set = StoredReadSet(scope.sha256, tuple(_batch(value) for value in plan['raw_read_set']))
        store = _store(plan['storage'])
        native = None
        if plan['format_version'] == 'quadringent-collection-plan-v2':
            processes['journal'], native = _native_journal(
                plan['journal'], scope, store, _object(plan['initial_copy']),
                _absolute(out_dir), inputs, remaining(),
            )
        result = collect_verification(
            _absolute(out_dir), scope, source=source, **processes, store=store,
            raw_prefix_root=plan['raw_prefix_root'], initial_copy=_object(plan['initial_copy']),
            copy_intent=_object(plan['copy_intent']), raw_read_set=read_set,
            connection_factory=lambda: _connect(destination, private_key), check_current=inputs.current,
            max_seconds=remaining(),
        )
        output = dict(status='comparison_recorded', comparison_state=result.report['state'],
                      report_path=str(result.report_reference.path),
                      collection_path=str(result.collection_reference.path), native_cdc_qualified=False,
                      native_admission='not_evaluated', context_enforcement='local_files_only')
        if native is not None:
            output['native_copy_inputs_path'] = str(_native_receipt(native, inputs, remaining()))
            if inputs.current() is not True:
                raise ValueError('changed_native_input')
        remaining()
        print(json.dumps(output), file=stdout)
        return 0 if result.report['state'] == 'verified' else 2
    except Exception:
        print(json.dumps(dict(status='incomplete', native_cdc_qualified=False,
                              reason_code='collection_unavailable')), file=stdout)
        return 1
