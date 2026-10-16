"""Export original stored row images; physical inventory admission is external.

RAW requires the controller's pinned read-set for the exact scope. Neither this
DTO nor framed EOF establishes a quiescent cut or a complete physical listing.
The caller retains the original references, including reserved RRN metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import math
import re
import time

from ..proof_windows import validate_bound_receipt
from ..raw import read_raw_batch
from ..receiver_registry import ReceiverNameRegistry
from ..storage_layout import journal_prefix, snapshot_prefix
from .verification_contract import VerificationScope
from .verification_stream import StreamLimits, StreamSummary, write_stream


_SHA = re.compile(r'[0-9a-f]{64}')
_BATCH = re.compile(r'batch-([0-9a-f]{32})\.manifest\.json')
_FIELDS = frozenset({'event_id', 'source_system', 'journal', 'library', 'table', 'operation',
                     'journal_receiver', 'journal_sequence', 'commit_timestamp', 'schema_version', 'before', 'after'})
_JAVA = frozenset({'journal_receiver_library', 'journal_entry_type'})
_ENTRY = {'c': {'ADD_ROW1', 'ADD_ROW2'}, 'u_before': {'BEFORE_IMAGE'},
          'u_after': {'AFTER_IMAGE'}, 'd': {'DELETE_ROW'}}


class StoredExportError(ValueError):
    """Stored input could not be exported; never include input values."""


def _key(value):
    if (type(value) is not str or not 1 <= len(value) <= 4096 or value.strip() != value
            or any(part in {'', '.', '..'} for part in value.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 or c == '\\' for c in value)):
        raise ValueError('invalid_object_key')


@dataclass(frozen=True)
class ObjectReference:
    key: str
    sha256: str

    def __post_init__(self):
        _key(self.key)
        if type(self.sha256) is not str or _SHA.fullmatch(self.sha256) is None:
            raise ValueError('invalid_object_reference')


@dataclass(frozen=True)
class BatchReference:
    manifest: ObjectReference
    payload: ObjectReference
    receipt: ObjectReference | None = None

    def __post_init__(self):
        if (type(self.manifest) is not ObjectReference or type(self.payload) is not ObjectReference
                or self.receipt is not None and type(self.receipt) is not ObjectReference):
            raise ValueError('invalid_batch_reference')


@dataclass(frozen=True)
class StoredReadSet:
    """Controller-supplied inventory, never a self-certified completeness proof."""

    scope_sha256: str
    batches: tuple[BatchReference, ...]

    def __post_init__(self):
        if (type(self.scope_sha256) is not str or _SHA.fullmatch(self.scope_sha256) is None
                or type(self.batches) is not tuple or len(self.batches) > 100_000
                or any(type(ref) is not BatchReference for ref in self.batches)):
            raise ValueError('invalid_stored_read_set')


@dataclass(frozen=True)
class StoredLimits:
    """Bounds include metadata reads and every repeated object reference."""

    max_object_bytes: int = 32 * 1024**2
    max_total_bytes: int = 16 * 1024**3
    max_objects: int = 100_000
    max_record_bytes: int = 8 * 1024**2

    def __post_init__(self):
        for value, maximum in ((self.max_object_bytes, 64 * 1024**2),
                               (self.max_total_bytes, 16 * 1024**3),
                               (self.max_objects, 100_000), (self.max_record_bytes, 8 * 1024**2)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('invalid_stored_limits')


class _Budget:
    def __init__(self, store, limits, seconds):
        self.store, self.limits, self.seconds = store, limits, seconds
        self.started = self.previous = time.monotonic()
        self.bytes = self.objects = 0

    def check(self):
        now = time.monotonic()
        if not math.isfinite(now) or now < self.previous or now - self.started >= self.seconds:
            raise ValueError('stored_deadline')
        self.previous = now

    def read(self, key, maximum=None):
        self.check()
        _key(key)
        if self.objects >= self.limits.max_objects or self.bytes >= self.limits.max_total_bytes:
            raise ValueError('stored_budget')
        maximum = min(maximum or self.limits.max_object_bytes, self.limits.max_object_bytes,
                      self.limits.max_total_bytes - self.bytes)
        data = self.store.get_bounded(key, maximum)
        self.check()
        if type(data) is not bytes or not 0 < len(data) <= maximum:
            raise ValueError('stored_object_budget')
        self.bytes += len(data)
        self.objects += 1
        return data

    def pinned(self, reference, maximum=None):
        if type(reference) is not ObjectReference:
            raise ValueError('stored_reference_required')
        data = self.read(reference.key, maximum)
        if hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ValueError('stored_digest_mismatch')
        return data


class _Prefix:
    def __init__(self, budget, prefix, pinned=None):
        self.budget, self.prefix, self.pinned = budget, prefix, pinned

    def get_bounded(self, key, max_bytes):
        _key(key)
        full_key = self.prefix + '/' + key
        if self.pinned is not None and full_key == self.pinned.key:
            return self.budget.pinned(self.pinned, max_bytes)
        return self.budget.read(full_key, max_bytes)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_stored_field')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('invalid_stored_number')


def _json(data):
    return json.loads(data, object_pairs_hook=_unique, parse_float=Decimal, parse_constant=_constant)


def _pair(manifest, payload, prefix):
    name = manifest.removeprefix(prefix + '/')
    match = _BATCH.fullmatch(name)
    if match is None or payload != prefix + '/batch-' + match[1] + '.jsonl':
        raise ValueError('stored_namespace_mismatch')


def _copy(budget, scope, root, initial_copy, copy_intent):
    from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, copy_intent_key, evidence_key

    if (initial_copy.key != evidence_key(root, scope.table_id, scope.copy_run_id)
            or copy_intent.key != 'receiver-authority-v1/' + copy_intent_key(
                scope.pipeline_id, scope.table_id, scope.copy_run_id)):
        raise ValueError('copy_namespace_mismatch')
    raw = _json(budget.pinned(initial_copy, 65536))
    if (type(raw) is not dict or raw.get('format_version') != 'quadringent-initial-copy-evidence-v2'
            or type(raw.get('snapshot_batches')) is not list
            or any(type(ref) is not dict or set(ref) != {'payload_key', 'manifest_key'}
                   or any(type(value) is not str for value in ref.values()) for ref in raw['snapshot_batches'])):
        raise ValueError('bound_copy_evidence_required')
    evidence = InitialCopyEvidence.from_dict(raw)
    if ((evidence.pipeline_id, evidence.table_id, evidence.run_id, evidence.rows_copied)
            != (scope.pipeline_id, scope.table_id, scope.copy_run_id, scope.expected_snapshot_rows)
            or evidence.require_bound() != scope.copy_boundary
            or evidence.boundary.capture_binding != scope.capture_binding):
        raise ValueError('copy_scope_mismatch')
    intent = _json(budget.pinned(copy_intent, 65536))
    if intent != {'format_version': 'quadringent-copy-intent-v1', 'pipeline_id': scope.pipeline_id,
                  'table_id': scope.table_id, 'run_id': scope.copy_run_id, 'boundary': evidence.boundary.to_dict()}:
        raise ValueError('original_copy_intent_mismatch')
    registry = ReceiverNameRegistry(_Prefix(budget, 'receiver-authority-v1'),
                                    authority=scope.capture_binding.registry_authority,
                                    source_system='ibmi', journal=scope.capture_binding.journal)
    if registry.load_binding(scope.capture_binding.scope_id) != scope.capture_binding:
        raise ValueError('original_binding_mismatch')
    registry.require(scope.capture_binding, scope.copy_boundary)
    registry.require(scope.capture_binding, scope.checkpoint)
    prefix = snapshot_prefix(root, scope.source_table)
    refs = tuple((prefix + '/' + ref.manifest_key, prefix + '/' + ref.payload_key)
                 for ref in evidence.snapshot_batches)
    if len(refs) > budget.limits.max_objects // 2 or len(set(refs)) != len(refs):
        raise ValueError('snapshot_inventory_invalid')
    for manifest, payload in refs:
        _pair(manifest, payload, prefix)
    return refs, registry


def _typed(value, kind):
    if value is None:
        return None
    if kind in {'int', 'decimal'}:
        if type(value) not in (int, str, Decimal):
            raise ValueError('inexact_stored_number')
        number = Decimal(value)
        if (not number.is_finite() or len(str(value)) > 1024
                or kind == 'int' and number != number.to_integral_value()):
            raise ValueError('invalid_stored_number')
    elif type(value) is not str:
        raise ValueError('invalid_stored_text')
    elif kind == 'date':
        date.fromisoformat(value)
    elif kind == 'timestamp':
        datetime.fromisoformat(value)
    return value


def _row(record, event, scope, snapshot):
    if type(record) is not dict or set(record) not in (_FIELDS, _FIELDS | _JAVA):
        raise ValueError('stored_envelope_fields')
    if (record['event_id'] != event.event_id or type(record['journal_sequence']) is not int
            or record['journal_sequence'] != event.position.sequence
            or (record['source_system'], record['library'], record['table'])
            != ('ibmi', scope.source_library, scope.source_table)):
        raise ValueError('stored_original_identity_mismatch')
    operation = record['operation']
    if operation not in _ENTRY or snapshot and operation != 'c':
        raise ValueError('unsupported_stored_operation')
    if snapshot:
        expected_journal = 'SNAPSHOT:' + scope.source_library + '.' + scope.source_table
        if record['journal_receiver'] != 'SNAPSHOT:' + scope.copy_run_id:
            raise ValueError('snapshot_run_mismatch')
        library, entry = 'SNAPSHOT', {'SNAPSHOT_ROW'}
        if not 1 <= event.position.sequence <= scope.expected_snapshot_rows:
            raise ValueError('snapshot_ordinal_outside_scope')
    else:
        expected_journal = scope.capture_binding.journal
        windows = [window for window in scope.receiver_windows
                   if window.identity.receiver_name == record['journal_receiver']]
        if len(windows) != 1 or not windows[0].start_sequence <= event.position.sequence <= windows[0].end_sequence:
            raise ValueError('stored_position_outside_scope')
        library, entry = windows[0].identity.receiver_library, _ENTRY[operation]
    if record['journal'] != expected_journal:
        raise ValueError('stored_journal_mismatch')
    if _JAVA <= set(record) and (record['journal_receiver_library'] != library
                                or record['journal_entry_type'] not in entry):
        raise ValueError('stored_native_metadata_mismatch')
    image = record['before'] if operation in {'u_before', 'd'} else record['after']
    names = {column.name for column in scope.columns}
    if type(image) is not dict or set(image) - names not in (set(), {'_rrn'}):
        raise ValueError('stored_business_fields_mismatch')
    if '_rrn' not in names and '_rrn' in image:
        if any(name.casefold() == '_rrn' for name in names) or type(image['_rrn']) is not int or image['_rrn'] < 0:
            raise ValueError('reserved_rrn_collision')
    if names - set(image):
        raise ValueError('stored_business_fields_missing')
    row = {column.name: _typed(image[column.name], column.kind) for column in scope.columns}
    return {**row, 'EVENT_ID': record['event_id'], 'OPERATION': operation,
            'JOURNAL_RECEIVER': record['journal_receiver'], 'JOURNAL_SEQUENCE': str(record['journal_sequence'])}


def _batch_rows(budget, scope, manifest_key, payload_key, *, snapshot, registry, reference=None):
    prefix = manifest_key.rsplit('/', 1)[0]
    _pair(manifest_key, payload_key, prefix)
    if reference is None:
        manifest, payload = budget.read(manifest_key), budget.read(payload_key)
    else:
        manifest, payload = budget.pinned(reference.manifest), budget.pinned(reference.payload)
    if (not payload.endswith(b'\n') or any(not line.strip() or len(line) > budget.limits.max_record_bytes
                                              for line in BytesIO(payload))):
        raise ValueError('invalid_stored_jsonl')
    batch = read_raw_batch(manifest, payload, preserve_decimals=True)
    expected_format = 'as400-snapshot-v2' if snapshot else 'as400-raw-v2'
    if (batch.manifest.format_version != expected_format or batch.manifest.capture_binding != scope.capture_binding
            or manifest_key.rsplit('/', 1)[1] != 'batch-' + batch.manifest.batch_id + '.manifest.json'):
        raise ValueError('stored_batch_binding_mismatch')
    if snapshot:
        if not batch.events or batch.manifest.high_watermark != batch.events[-1].position:
            raise ValueError('snapshot_watermark_mismatch')
        if batch.manifest.copy_context != {'pipeline_id': scope.pipeline_id, 'table_id': scope.table_id,
                                          'run_id': scope.copy_run_id, 'boundary': scope.copy_boundary.to_dict()}:
            raise ValueError('snapshot_copy_context_mismatch')
    else:
        registry.require(scope.capture_binding, batch.manifest.bound_high_watermark)
        window = next((w for w in scope.receiver_windows
                       if w.identity == batch.manifest.bound_high_watermark.receiver_identity), None)
        if window is None or not window.start_sequence <= batch.manifest.high_watermark.sequence <= window.end_sequence:
            raise ValueError('stored_watermark_outside_scope')
    if reference is not None and reference.receipt is not None:
        if snapshot or not reference.receipt.key.startswith(prefix + '/receipts/'):
            raise ValueError('stored_receipt_namespace')
        budget.pinned(reference.receipt, 65536)
        receipt = validate_bound_receipt(_Prefix(budget, prefix, reference.receipt),
                                         reference.receipt.key.removeprefix(prefix + '/'), scope.capture_binding)
        if (receipt['raw'] != {'manifest_key': manifest_key.rsplit('/', 1)[1],
                              'payload_key': payload_key.rsplit('/', 1)[1],
                              'manifest_sha256': hashlib.sha256(manifest).hexdigest(),
                              'payload_sha256': hashlib.sha256(payload).hexdigest()}
                or receipt['end'] != batch.manifest.bound_high_watermark.to_dict()
                or receipt['event_count'] != batch.manifest.event_count):
            raise ValueError('stored_receipt_batch_mismatch')
        if any(event.position.sequence < receipt['start']['sequence'] for event in batch.events):
            raise ValueError('stored_event_outside_receipt')
    lines = (line for line in BytesIO(payload) if line.strip())
    for event, line in zip(batch.events, lines, strict=True):
        budget.check()
        if len(line) > budget.limits.max_record_bytes:
            raise ValueError('stored_record_budget')
        yield _row(_json(line), event, scope, snapshot)


def export_stored(store, scope: VerificationScope, stream, *, dataset: str, raw_prefix_root: str,
                  initial_copy: ObjectReference, copy_intent: ObjectReference,
                  raw_read_set: StoredReadSet | None = None, limits: StreamLimits = StreamLimits(),
                  stored_limits: StoredLimits = StoredLimits()) -> StreamSummary:
    """Read bounded original batches without listing, deduplication or source calls.

    The caller admits inventory completeness and store namespace separately.
    Require successful return as well as framed EOF, including output I/O errors.
    """
    try:
        if (type(scope) is not VerificationScope or dataset not in {'snapshot', 'raw'}
                or type(limits) is not StreamLimits or type(stored_limits) is not StoredLimits
                or type(initial_copy) is not ObjectReference or type(copy_intent) is not ObjectReference):
            raise ValueError('invalid_stored_configuration')
        root = raw_prefix_root
        if root:
            _key(root)
        if type(root) is not str:
            raise ValueError('invalid_stored_root')
        budget = _Budget(store, stored_limits, limits.max_seconds)
        if dataset == 'raw' and (type(raw_read_set) is not StoredReadSet or raw_read_set.scope_sha256 != scope.sha256):
            raise ValueError('controller_read_set_required')
        snapshots, registry = _copy(budget, scope, root, initial_copy, copy_intent)
        if dataset == 'snapshot':
            refs = [(manifest, payload, True, None) for manifest, payload in snapshots]
        else:
            if len(raw_read_set.batches) > stored_limits.max_objects // 2:
                raise ValueError('stored_inventory_budget')
            snapshot_root, journal_root = snapshot_prefix(root, scope.source_table), journal_prefix(root, scope.source_table)
            refs = []
            for ref in raw_read_set.batches:
                is_snapshot = ref.manifest.key.startswith(snapshot_root + '/')
                _pair(ref.manifest.key, ref.payload.key, snapshot_root if is_snapshot else journal_root)
                refs.append((ref.manifest.key, ref.payload.key, is_snapshot, ref))
            if not set(snapshots).issubset({(manifest, payload) for manifest, payload, _, _ in refs}):
                raise ValueError('stored_original_snapshot_missing')

        def rows():
            intervals = []
            count = 0
            for manifest, payload, snapshot, reference in refs:
                first = last = None
                for row in _batch_rows(budget, scope, manifest, payload, snapshot=snapshot,
                                       registry=registry, reference=reference):
                    if dataset == 'snapshot':
                        sequence = int(row['JOURNAL_SEQUENCE'])
                        if last is not None and sequence != last + 1:
                            raise ValueError('snapshot_ordinal_gap_or_replay')
                        first = sequence if first is None else first
                        last = sequence
                    count += 1
                    yield row
                if dataset == 'snapshot' and first is not None:
                    intervals.append((first, last))
            if dataset == 'snapshot':
                cursor = 1
                for first, last in sorted(intervals):
                    if first != cursor:
                        raise ValueError('snapshot_inventory_gap_or_replay')
                    cursor = last + 1
                if count != scope.expected_snapshot_rows or cursor != count + 1:
                    raise ValueError('snapshot_inventory_population')
            budget.check()

        summary = write_stream(stream, rows(), scope_sha256=scope.sha256, dataset=dataset,
                               started_at=datetime.now(timezone.utc).isoformat(),
                               completed_at=lambda: datetime.now(timezone.utc).isoformat(), limits=limits)
        budget.check()
        stream.flush()
        budget.check()
        return summary
    except Exception:
        raise StoredExportError('stored_export_failed') from None
