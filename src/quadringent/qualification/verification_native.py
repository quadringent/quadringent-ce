"""Build the native journal invocation from a pinned v3 initial copy."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import struct

from quadringent.checkpoint import _decode_checkpoint_json
from quadringent.copy_observation import (
    ObservationReference, context_sha256, read_copy_observations, validate_observation,
)
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence

from .verification_collect import ProcessSpec, WindowProcess
from .verification_contract import VerificationScope

_MAX_BUNDLE = 393600
_NAME = re.compile(r'[A-Z0-9_$#@]{1,10}')


@dataclass(frozen=True)
class NativeCopyInputs:
    scope_sha256: str
    context_sha256: str
    binding_sha256: str
    boundary_sha256: str
    member: str
    format_name: str
    jid_hex: str
    bundle: bytes = field(repr=False)
    bundle_sha256: str
    native_admission: str = 'not_evaluated'


def _pinned(store, reference):
    data = store.get_bounded(reference.key, reference.byte_count)
    if (type(data) is not bytes or len(data) != reference.byte_count
            or hashlib.sha256(data).hexdigest() != reference.sha256):
        raise ValueError('native_copy_original_mismatch')
    return data


def load_native_copy_inputs(authority_store, evidence_bytes: bytes, *, expected_sha256: str,
                            evidence_key: str, scope: VerificationScope) -> NativeCopyInputs:
    if (type(scope) is not VerificationScope or type(evidence_bytes) is not bytes
            or not 1 <= len(evidence_bytes) <= 65536
            or hashlib.sha256(evidence_bytes).hexdigest() != expected_sha256):
        raise ValueError('native_copy_evidence_mismatch')
    evidence = InitialCopyEvidence.from_dict(_decode_checkpoint_json(evidence_bytes))
    if evidence.copy_observations is None:
        raise ValueError('native_copy_observations_required')
    if ((evidence.pipeline_id, evidence.table_id, evidence.run_id, evidence.rows_copied)
            != (scope.pipeline_id, scope.table_id, scope.copy_run_id, scope.expected_snapshot_rows)
            or evidence.require_bound() != scope.copy_boundary
            or evidence.boundary.capture_binding != scope.capture_binding):
        raise ValueError('native_copy_scope_mismatch')
    manifest = read_copy_observations(
        authority_store, evidence.copy_observations,
        expected_batches=[value.to_dict() for value in evidence.snapshot_batches],
    )
    context = manifest['context']
    if (manifest['evidence_key'] != evidence_key or manifest['rows_copied'] != evidence.rows_copied
            or (context['pipeline_id'], context['table_id'], context['run_id'], context['boundary'])
            != (scope.pipeline_id, scope.table_id, scope.copy_run_id, evidence.boundary.to_dict())
            or (context['request']['source']['library'], context['request']['source']['file'])
            != (scope.source_library, scope.source_table)):
        raise ValueError('native_copy_context_mismatch')
    bundle = bytearray(b'QDTNCB2\n')
    documents = {}
    for phase in ('before', 'after'):
        raw = _pinned(authority_store, ObservationReference.from_dict(manifest[phase]))
        document = validate_observation(_decode_checkpoint_json(raw), context=context, phase=phase)
        if document['format_version'] != 'quadringent-native-copy-observation-v2':
            raise ValueError('native_copy_file_observation_required')
        if tuple((value['sql_name'], value['kind']) for value in document['format']['fields']) != tuple(
                (value.name, value.kind) for value in scope.columns):
            raise ValueError('native_copy_columns_mismatch')
        originals = {value['id']: value for value in document['originals']}
        bundle.extend(struct.pack('>iii', *(originals[name]['ccsid'] for name in ('file', 'format', 'rcac'))))
        for name in ('file', 'format', 'rcac'):
            for key, length in (('receiver', None), ('qualified_target', 20), ('error', 16)):
                value = base64.b64decode(originals[name][key + '_base64'], validate=True)
                if (not 1 <= len(value) <= 65536 or length is not None and len(value) != length
                        or hashlib.sha256(value).hexdigest() != originals[name][key + '_sha256']):
                    raise ValueError('native_copy_descriptor_mismatch')
                bundle.extend(struct.pack('>I', len(value)))
                bundle.extend(value)
        documents[phase] = document
    if len(bundle) > _MAX_BUNDLE:
        raise ValueError('native_copy_bundle_budget')
    source = context['request']['source']
    return NativeCopyInputs(
        scope.sha256, context_sha256(context), context_sha256(context['boundary']['capture_binding']),
        context_sha256(context['boundary']), source['member'], source['format'],
        documents['before']['member']['jid_hex'], bytes(bundle), hashlib.sha256(bundle).hexdigest(),
    )


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_copy_absolute_path_required')
    return str(path)


def native_journal_processes(copy: NativeCopyInputs, scope: VerificationScope, *, java: str,
                             classpath: str, credentials: bytes, environment, bundle_path: Path,
                             guard_receipt_path: Path, max_seconds: int = 900) -> tuple[WindowProcess, ...]:
    if (type(copy) is not NativeCopyInputs or copy.scope_sha256 != scope.sha256
            or copy.native_admission != 'not_evaluated' or len(scope.receiver_windows) != 1
            or type(max_seconds) is not int or not 1 <= max_seconds <= 900
            or type(classpath) is not str or not 1 <= len(classpath) <= 8192
            or '\0' in classpath):
        raise ValueError('unsupported_native_journal_plan')
    for name in (scope.source_library, scope.source_table, copy.member, copy.format_name):
        if _NAME.fullmatch(name) is None:
            raise ValueError('unsupported_native_source_name')
    if any(value.kind not in {'int', 'decimal', 'text'} for value in scope.columns):
        raise ValueError('unsupported_native_columns')
    window = scope.receiver_windows[0]
    identity = window.identity
    boundary = scope.copy_boundary
    if (identity != boundary.receiver_identity or scope.checkpoint.receiver_identity != identity
            or window.start_sequence != boundary.sequence + 1):
        raise ValueError('unsupported_native_receiver_rotation')
    for value in (boundary.sequence, window.start_sequence, window.end_sequence):
        if not 0 <= value <= 10**21 - 1:
            raise ValueError('unsupported_native_sequence')
    command = (
        _absolute(java), '-cp', classpath, 'io.quadringent.as400.NativeBusinessImageExport',
        identity.journal_library, identity.journal_name, identity.receiver_library, identity.receiver_name,
        scope.source_library, scope.source_table, copy.member, copy.jid_hex,
        str(window.start_sequence), str(window.end_sequence), scope.sha256, scope.copy_run_id,
        copy.binding_sha256, copy.format_name, ','.join(value.name for value in scope.columns),
        ','.join(value.kind for value in scope.columns), copy.context_sha256, copy.boundary_sha256,
        boundary.receiver_identity.receiver_library, boundary.receiver_identity.receiver_name, str(boundary.sequence),
        _absolute(bundle_path), copy.bundle_sha256, _absolute(guard_receipt_path),
        '2000000', str(2 * 1024**3), str(max_seconds),
    )
    return (WindowProcess(window, ProcessSpec(command, credentials, environment)),)
