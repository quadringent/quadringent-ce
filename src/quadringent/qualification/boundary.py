"""Read original copy boundaries without replacing their native envelopes."""
from __future__ import annotations

from dataclasses import dataclass

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import EVIDENCE_FORMAT_VERSION, InitialCopyEvidence


@dataclass(frozen=True)
class LegacyBoundary:
    receiver_library: str
    receiver_name: str
    last_sequence: int
    capture_binding: None = None


def legacy_inspection_allowed(args, *, historical=False):
    # Unadmitted library inspection remains distinct from installed runtime qualification.
    return not getattr(args, 'expected_revision', None) or bool(historical and (
        getattr(args, 'release_admission', None) or getattr(args, 'initial_copy_admission', None)))


def scope_context(args):
    return {name: getattr(args, name, None) for name in (
        'pipeline', 'table', 'table_id', 'copy_run_id', 'copy_boundary', 'journal_library', 'journal_name',
        'expected_revision', 'release_admission', 'initial_copy_revision', 'initial_copy_admission')}


def read_binding(value, args, *, authority=None, scope_id=None):
    binding = value if type(value) is CaptureBinding else CaptureBinding.from_dict(value)
    identity = binding.bootstrap.receiver_identity
    if (binding.source_system != 'ibmi'
            or getattr(args, 'journal_library', None) is not None and identity.journal_library != args.journal_library
            or getattr(args, 'journal_name', None) is not None and binding.journal != args.journal_name
            or authority is not None and binding.registry_authority != authority
            or scope_id is not None and binding.scope_id != scope_id):
        raise ValueError('capture binding differs from the original native runtime scope')
    return binding


def read_boundary(value, *, allow_legacy=False, journal_library=None, journal_name=None, expected_binding=None):
    if not isinstance(value, dict):
        raise ValueError('original copy boundary is missing')
    if 'format_version' in value:
        boundary = JournalBoundary.from_dict(value)
        point = boundary.require_bound()
        binding = boundary.capture_binding
        bootstrap = binding.bootstrap
        if (binding.source_system != 'ibmi'
                or journal_library is not None and point.receiver_identity.journal_library != journal_library
                or journal_name is not None and (point.receiver_identity.journal_name != journal_name or binding.journal != journal_name)
                or expected_binding is not None and binding != expected_binding
                or point.receiver_identity.receiver_name == bootstrap.receiver_identity.receiver_name
                and (point.receiver_identity != bootstrap.receiver_identity or point.sequence < bootstrap.sequence)):
            raise ValueError('native copy boundary belongs to another journal or capture binding')
        return boundary
    if not allow_legacy or expected_binding is not None:
        raise ValueError('installed qualification requires a bound original copy boundary')
    if set(value) - {'receiver_library', 'receiver_name', 'last_sequence', 'observed_at'}:
        raise ValueError('native copy boundary cannot be downgraded to historical inspection')
    library, name, sequence = (value.get(key) for key in ('receiver_library', 'receiver_name', 'last_sequence'))
    if not isinstance(library, str) or not library or not isinstance(name, str) or not name or type(sequence) is not int or sequence < 0:
        raise ValueError('legacy inspection boundary is incomplete')
    return LegacyBoundary(library, name, sequence)


def read_copy_boundary(evidence, args, *, expected_binding=None, original_only=False, historical=False):
    if (not isinstance(evidence, dict) or any(evidence.get(key) != getattr(args, attr) for key, attr in (
            ('pipeline_id', 'pipeline'), ('table_id', 'table_id'), ('run_id', 'copy_run_id')))):
        raise ValueError('copy evidence belongs to another pipeline/table/run')
    boundary = read_boundary(evidence.get('boundary'), allow_legacy=legacy_inspection_allowed(args, historical=historical),
                             journal_library=getattr(args, 'journal_library', None),
                             journal_name=getattr(args, 'journal_name', None), expected_binding=expected_binding)
    if boundary.capture_binding is not None or 'format_version' in evidence:
        fields = {'format_version', 'pipeline_id', 'table_id', 'run_id', 'boundary', 'rows_copied', 'completed_at', 'snapshot_batches'}
        if evidence.get('format_version') != EVIDENCE_FORMAT_VERSION or not fields <= set(evidence):
            raise ValueError('original bound copy evidence format is missing')
        InitialCopyEvidence.from_dict(evidence if original_only else {key: evidence[key] for key in fields}).require_bound()
    if boundary.receiver_name + ':' + str(boundary.last_sequence) != args.copy_boundary:
        raise ValueError('copy boundary differs from the admitted original position')
    return boundary


def read_checkpoint_record(value):
    if not isinstance(value, dict):
        raise ValueError('native checkpoint is missing')
    point = BoundCheckpoint.from_dict(value.get('bound_checkpoint'))
    binding = read_binding(value.get('capture_binding'), None)
    binding.validate_checkpoint(point)
    bootstrap = binding.bootstrap
    if (type(value.get('receiver')) is not str or type(value.get('sequence')) is not int
            or value['receiver'] != point.receiver_identity.receiver_name or value['sequence'] != point.sequence
            or point.receiver_identity.receiver_name == bootstrap.receiver_identity.receiver_name
            and (point.receiver_identity != bootstrap.receiver_identity or point.sequence < bootstrap.sequence)):
        raise ValueError('checkpoint position or native receiver generation differs')
    return binding, point


def read_checkpoint(value, boundary):
    if boundary.capture_binding is None:
        return None
    binding, point = read_checkpoint_record(value)
    if binding != boundary.capture_binding:
        raise ValueError('checkpoint belongs to another original capture binding')
    original = boundary.require_bound()
    if (point.receiver_identity.receiver_name == original.receiver_identity.receiver_name
            and (point.receiver_identity != original.receiver_identity or point.sequence < original.sequence)):
        raise ValueError('checkpoint position or native receiver generation differs')
    return point


def read_checkpoint_pair(before, after):
    old_binding, old = read_checkpoint_record(before)
    new_binding, new = read_checkpoint_record(after)
    if (old_binding != new_binding
            or old.receiver_identity.receiver_name == new.receiver_identity.receiver_name
            and (old.receiver_identity != new.receiver_identity or new.sequence < old.sequence)):
        raise ValueError('native checkpoint capture binding or receiver identity changed during recovery')
    return old, new


def installed_checkpoints_required(*snapshots):
    return any(any(field in snapshot.get('checkpoint', {}) for field in ('bound_checkpoint', 'capture_binding'))
               or any(snapshot.get(component, {}).get('image_binding', {}).get('verified') is True
                      for component in ('reader', 'loader')) for snapshot in snapshots)
