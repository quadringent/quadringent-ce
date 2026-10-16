"""Reparse archived native observations offline; source provenance remains separate."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct

from quadringent.copy_observation import (
    _job, _timestamp, context_sha256, validate_observation, validate_observation_pair,
)
from .verification_contract import VerificationScope
from .verification_guard import _Budget, _document, _guard_document, _parent, _read_original
from .verification_native import NativeCopyInputs
from .verification_process import DocumentLimits, _new_directory, collect_process_document

_CLASS = 'io.quadringent.as400.NativeSourceObservationVerify'
_ENV = {'PATH', 'JAVA_HOME', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR'}
_MAX = 4 * 1024**2
_ENTRY = ('receiver_library', 'receiver_name', 'sequence', 'journal_code', 'journal_entry_type', 'object',
          'object_type', 'jid_hex', 'minimized_entry_data', 'minimized_on_field_boundary', 'indicator_flag',
          'null_value_indicators', 'declared_payload_bytes', 'entry_data_bytes', 'entry_data_base64', 'entry_data_sha256')


def _require(value):
    if not value:
        raise ValueError('invalid_native_source_observations')


def _fields(value, fields):
    _require(type(value) is dict and set(value) == set(fields.split()))


def _binary(value, digest, maximum, count=None):
    _require(type(value) is str and len(value) <= maximum * 2 and type(digest) is str)
    raw = base64.b64decode(value, validate=True)
    _require(len(raw) <= maximum and base64.b64encode(raw).decode() == value
             and hashlib.sha256(raw).hexdigest() == digest and (count is None or type(count) is int and count == len(raw)))
    return raw


def _sequence(value):
    _require(type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,20}', value) is not None)
    return int(value)


def _blob(raw):
    return struct.pack('>I', len(raw)) + raw


def _text(value):
    return _blob(value.encode('ascii'))


def _native(document):
    parts = {item['id']: item for item in document['originals']}
    data = b''.join(struct.pack('>i', parts[name]['ccsid']) for name in ('file', 'format', 'rcac'))
    for name in ('file', 'format', 'rcac'):
        for key in ('receiver', 'qualified_target', 'error'):
            item = parts[name]
            data += _blob(_binary(item[key + '_base64'], item[key + '_sha256'], 65536))
    return data


def _jobs(document):
    job = document['originals'][0]['job_before']
    _job(document['jdbc_job'])
    for original in document['originals']:
        _require(original['job_before'] == original['job_after'] == job)
    return job


def _receipts(copy, scope, journal_raw, final_raw):
    pins = dict(scope_sha256=scope.sha256, context_sha256=copy.context_sha256,
        capture_binding_sha256=copy.binding_sha256, copy_boundary_sha256=copy.boundary_sha256,
        copy_run_id=scope.copy_run_id, bundle_sha256=copy.bundle_sha256)
    identity = scope.checkpoint.receiver_identity
    boundary = dict(receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
                    sequence=str(scope.copy_boundary.sequence))
    window = dict(receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
                  start_sequence=str(scope.copy_boundary.sequence + 1), end_sequence=str(scope.checkpoint.sequence))
    receipts = []
    for raw, mode in ((journal_raw, 'journal'), (final_raw, 'after_all_streams')):
        _require(type(raw) is bytes and 1 <= len(raw) <= _MAX)
        document = _guard_document(pins, _document(raw), mode)
        _fields(document, 'format_version mode state scope_sha256 context_sha256 capture_binding_sha256 copy_run_id '
                'copy_boundary_sha256 bundle_sha256 copy_boundary business_window copy_layout_sha256 guards '
                'current_observations error_code native_admission transaction_consistency_verified')
        _require(document['copy_boundary'] == boundary and document['business_window'] == window)
        receipts.append(document)
    return receipts


def _observations(copy, scope, receipts):
    _require(type(copy.initial_context) is bytes and 1 <= len(copy.initial_context) <= 1024**2
             and type(copy.initial_observations) is tuple and len(copy.initial_observations) == 2)
    context = _document(copy.initial_context)
    _require(context_sha256(context) == copy.context_sha256)
    original = validate_observation_pair(context=context, before_bytes=copy.initial_observations[0],
                                         after_bytes=copy.initial_observations[1])
    _require((context['pipeline_id'], context['table_id'], context['run_id'])
             == (scope.pipeline_id, scope.table_id, scope.copy_run_id)
             and context['boundary']['bound_checkpoint'] == scope.copy_boundary.to_dict()
             and context['boundary']['capture_binding'] == scope.capture_binding.to_dict()
             and context['request']['source'] == dict(library=scope.source_library, file=scope.source_table,
                                                     member=copy.member, format=copy.format_name))
    documents = [original['before'], original['after']]
    bundle = b'QDTNCB2\n' + b''.join(_native(d) for d in documents)
    _require(bundle == copy.bundle and hashlib.sha256(bundle).hexdigest() == copy.bundle_sha256
             and context_sha256(context['boundary']) == copy.boundary_sha256
             and context_sha256(context['boundary']['capture_binding']) == copy.binding_sha256)
    identity = scope.checkpoint.receiver_identity
    witness_window = dict(receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
                          start='1', end=str(max(scope.checkpoint.sequence, scope.copy_boundary.sequence)))
    for receipt in receipts:
        phases = ['before_business', 'after_business'] if receipt['mode'] == 'journal' else ['after_all_streams']
        _require(type(receipt['current_observations']) is list and len(receipt['current_observations']) == len(phases)
                 and receipt['copy_layout_sha256'] == documents[0]['format']['layout_sha256'])
        for index, (phase, item) in enumerate(zip(phases, receipt['current_observations'], strict=True)):
            _fields(item, 'phase metadata_bytes metadata_sha256 metadata_base64 layout_sha256')
            _require(item['phase'] == phase and type(item['metadata_bytes']) is int and item['metadata_bytes'] >= 1)
            raw = _binary(item['metadata_base64'], item['metadata_sha256'], 1024**2, item['metadata_bytes'])
            document = validate_observation(_document(raw), context=context,
                phase='after' if index == 1 else 'before', witness_window=witness_window)
            _require(document['format']['layout_sha256'] == item['layout_sha256'])
            documents.append(document)
    for document in documents:
        _jobs(document)
        _require(document['member']['jid_hex'] == copy.jid_hex
                 and [(f['sql_name'], f['kind']) for f in document['format']['fields']]
                 == [(column.name, column.kind) for column in scope.columns])
        _require({k: document['format'][k] for k in ('level_id', 'record_length', 'fields')}
                 == {k: documents[0]['format'][k] for k in ('level_id', 'record_length', 'fields')})
    _require(documents[2]['jdbc_job'] == documents[3]['jdbc_job'] and _jobs(documents[2]) == _jobs(documents[3]))
    _require(_timestamp(documents[2]['completed_at']) <= _timestamp(documents[3]['started_at'])
             and _timestamp(documents[3]['completed_at']) <= _timestamp(documents[4]['started_at']))
    return documents


def _guards(scope, copy, receipts, documents):
    result = []
    identity = scope.checkpoint.receiver_identity
    previous_prefix = scope.checkpoint.sequence
    for guard, document in zip([g for r in receipts for g in r['guards']], documents[2:], strict=True):
        _fields(guard, 'phase prefix coverage entries')
        prefix, coverage, entries = guard['prefix'], guard['coverage'], guard['entries']
        _fields(prefix, 'journal_library journal_name receiver_library receiver_name sequence asp_device program format '
                'force_key force_value entry_data_length journal_type journal_state sql_job request_parameters '
                'job_before job_after receiver_base64 receiver_sha256 error_base64 error_sha256 transaction_cut')
        _require((prefix['journal_library'], prefix['journal_name'], prefix['receiver_library'], prefix['receiver_name'])
                 == (identity.journal_library, identity.journal_name, identity.receiver_library, identity.receiver_name)
                 and prefix['program'] == 'QJOSJRNE' and prefix['format'] == 'SJNE0100'
                 and type(prefix['force_key']) is int and prefix['force_key'] == 4 and prefix['force_value'] == '2'
                 and type(prefix['entry_data_length']) is int and prefix['entry_data_length'] == 0
                 and prefix['journal_type'] == '*LOCAL' and prefix['journal_state'] == '*ACTIVE'
                 and prefix['transaction_cut'] is False and prefix['sql_job'] == document['jdbc_job']
                 and prefix['job_before'] == prefix['job_after'] == _jobs(document))
        end = _sequence(prefix['sequence'])
        _require(end >= previous_prefix)
        previous_prefix = end
        _fields(coverage, 'receiver_library receiver_name start_sequence end_sequence journal_codes state row_count rows_sha256')
        _require(coverage == dict(receiver_library=identity.receiver_library, receiver_name=identity.receiver_name,
            start_sequence=str(scope.copy_boundary.sequence + 1), end_sequence=str(end), journal_codes='D,F,R',
            state='complete', row_count=coverage['row_count'], rows_sha256=coverage['rows_sha256'])
            and type(entries) is list and len(entries) <= 2_000_000 and type(coverage['row_count']) is int
            and coverage['row_count'] == len(entries))
        measured = hashlib.sha256()
        previous = scope.copy_boundary.sequence
        for item in entries:
            _require(type(item) is dict and set(item) == set(_ENTRY))
            sequence = _sequence(item['sequence'])
            _require(previous < sequence <= end and item['receiver_library'] == identity.receiver_library
                     and item['receiver_name'] == identity.receiver_name)
            previous = sequence
            count = item['declared_payload_bytes']
            if count is None:
                _require(all(item[k] is None for k in ('entry_data_bytes', 'entry_data_base64', 'entry_data_sha256')))
                data = None
            else:
                _require(type(count) is int and 0 <= count <= 65536 and type(item['entry_data_bytes']) is int)
                data = _binary(item['entry_data_base64'], item['entry_data_sha256'], 65536, count)
                _require(item['entry_data_bytes'] == count)
            if not (item['journal_code'] == 'F' and item['journal_entry_type'] in {'OP', 'CL'}):
                _require(item['journal_code'] == 'R' and item['journal_entry_type'] in {'PT', 'PX', 'UB', 'UP', 'DL'}
                    and item['object'] == scope.source_table.ljust(10) + scope.source_library.ljust(10) + copy.member.ljust(10)
                    and item['object_type'] == '*FILE' and type(item['jid_hex']) is str
                    and item['jid_hex'].lower() == copy.jid_hex and data is not None
                    and len(data) == document['format']['record_length']
                    and item['minimized_entry_data'] == item['minimized_on_field_boundary'] == '0'
                    and type(item['null_value_indicators']) is str
                    and (re.fullmatch(' *', item['null_value_indicators']) is not None
                         or item['null_value_indicators'] == '0' * len(scope.columns))
                    and (item['journal_entry_type'] not in {'UB', 'DL'} or item['indicator_flag'] == '1'))
            measured.update((json.dumps({k: item[k] for k in _ENTRY}, separators=(',', ':'),
                                        ensure_ascii=False, allow_nan=False) + '\n').encode())
        _require(measured.hexdigest() == coverage['rows_sha256'])
        raw = _binary(prefix['receiver_base64'], prefix['receiver_sha256'], 58, 58)
        error = _binary(prefix['error_base64'], prefix['error_sha256'], 16, 16)
        _require(error == struct.pack('>i', 16) + bytes(12))
        result.append((prefix, struct.pack('>i', _jobs(document)['ccsid']) + _blob(raw) + _blob(error)))
    return result


def _payload(copy, scope, documents, prefixes):
    identity = scope.checkpoint.receiver_identity
    values = (identity.journal_library, identity.journal_name, identity.receiver_library, identity.receiver_name,
              scope.source_library, scope.source_table, copy.member, copy.jid_hex, str(scope.copy_boundary.sequence + 1),
              str(scope.checkpoint.sequence), scope.sha256, scope.copy_run_id, copy.binding_sha256, copy.format_name)
    data = b'QDTNSO1\n' + b''.join(_text(v) for v in values) + struct.pack('>i', len(scope.columns))
    data += b''.join(_text(c.name) + _text(c.kind) for c in scope.columns)
    for document in documents:
        data += _native(document)
        window = document['witness_window']
        data += _text(window['start_sequence']) + _text(window['end_sequence'])
        for original in document['originals'][3:]:
            for key in ('receiver', 'error'):
                data += _blob(_binary(original[key + '_base64'], original[key + '_sha256'], 65536))
    return data + b''.join(raw for _, raw in prefixes)


def validate_source_observations(copy: NativeCopyInputs, scope: VerificationScope, journal_original: bytes,
                                 final_original: bytes, *, java, classpath, environment, directory,
                                 check_current, max_seconds=60) -> dict:
    """Reparse existing originals with a server-selected offline JVM, never source credentials."""
    root = identity = parent = None
    try:
        budget = _Budget(max_seconds, check_current)
        _require(type(copy) is NativeCopyInputs and type(scope) is VerificationScope and copy.scope_sha256 == scope.sha256
                 and copy.native_admission == 'not_evaluated' and len(scope.receiver_windows) == 1
                 and scope.copy_boundary.receiver_identity == scope.checkpoint.receiver_identity
                 and scope.receiver_windows[0].identity == scope.checkpoint.receiver_identity
                 and scope.receiver_windows[0].start_sequence == scope.copy_boundary.sequence + 1
                 and type(java) is str and Path(java).is_absolute() and '..' not in Path(java).parts
                 and type(classpath) is str and 1 <= len(classpath) <= 8192
                 and all(Path(p).is_absolute() and '..' not in Path(p).parts for p in classpath.split(os.pathsep))
                 and type(environment) is dict and set(environment) <= _ENV)
        receipts = _receipts(copy, scope, journal_original, final_original)
        documents = _observations(copy, scope, receipts)
        prefixes = _guards(scope, copy, receipts, documents)
        payload = _payload(copy, scope, documents, prefixes)
        _require(len(payload) <= 3 * 1024**2)
        budget.check()
        root = _new_directory(directory)
        parent = _parent(root)
        metadata = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        identity = metadata.st_dev, metadata.st_ino
        path = root / 'originals.bin'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        sealed, original, inode = _read_original(path, 3 * 1024**2, budget)
        _require(sealed == payload)
        result = collect_process_document((java, '-Xmx128m', '-cp', classpath, _CLASS, str(path), original.sha256),
            directory=root / 'offline', input_bytes=b'', environment=environment, validate=lambda value: None,
            limits=DocumentLimits(max_bytes=_MAX, max_seconds=max(1, int(budget.check()))))
        raw, observed, _ = _read_original(result.path, _MAX, budget)
        _require(type(result.return_code) is int and result.return_code == 0 and observed.sha256 == result.sha256
                 and observed.byte_count == result.byte_count)
        output = _document(raw)
        _fields(output, 'format_version scope_sha256 observations prefixes native_admission')
        _require(output['format_version'] == 'quadringent-native-source-offline-v1'
                 and output['scope_sha256'] == scope.sha256 and output['native_admission'] == 'not_evaluated'
                 and type(output['observations']) is list and len(output['observations']) == 5
                 and type(output['prefixes']) is list and len(output['prefixes']) == 3)
        for document, parsed in zip(documents, output['observations'], strict=True):
            expected = dict(level_id=document['format']['level_id'], record_length=document['format']['record_length'],
                            layout_sha256=document['format']['layout_sha256'], fields=[
                {k: v for k, v in f.items() if k not in {'sql_name', 'kind'}} for f in document['format']['fields']])
            _require(parsed == expected)
            _require(all(f['kind'] == {'0000': 'int', '0003': 'decimal', '0004': 'text'}[f['native_type']]
                         for f in document['format']['fields']))
        for (prefix, _), parsed in zip(prefixes, output['prefixes'], strict=True):
            _require(parsed == {k: prefix[k] for k in ('receiver_library', 'receiver_name', 'sequence', 'asp_device',
                                                      'request_parameters')})
        after, current, changed = _read_original(path, 3 * 1024**2, budget)
        _require(after == payload and current == original and changed == inode)
        budget.check()
        return dict(scope_sha256=scope.sha256, copy_context_sha256=copy.context_sha256,
                    verification_context_sha256=context_sha256(dict(scope=scope.to_dict(),
                        initial_context_sha256=copy.context_sha256, witness_window=documents[2]['witness_window'])),
                    validated_current_observations=3, validated_guards=3, native_admission='not_evaluated',
                    source_provenance='not_evaluated', transaction_consistency_verified=False)
    except Exception:
        raise ValueError('native_source_observations_failed') from None
    finally:
        if parent is not None:
            try:
                if identity is not None:
                    current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
                    _require((current.st_dev, current.st_ino) == identity)
                    shutil.rmtree(root.name, dir_fd=parent)
            except Exception:
                raise ValueError('native_source_observations_failed') from None
            finally:
                os.close(parent)
