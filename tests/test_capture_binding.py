from dataclasses import FrozenInstanceError, replace

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity


IDENTITY = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "native-test-v1", "generation-a")
POINT = BoundCheckpoint("scope-a", "epoch-a", IDENTITY, 20)
AUTHORITY = "s3://registry-bucket/receiver-authority-v1"
BINDING = CaptureBinding("scope-a", "epoch-a", "ibmi", "JRN", AUTHORITY, POINT)


def test_binding_round_trip_preserves_the_original_bootstrap():
    record = BINDING.to_dict()
    assert record["format_version"] == "quadringent-capture-binding-v1"
    assert CaptureBinding.from_dict(record) == BINDING
    later = BINDING.checkpoint(IDENTITY, 30)
    assert later == replace(POINT, sequence=30)
    assert BINDING.bootstrap == POINT
    with pytest.raises(FrozenInstanceError):
        BINDING.bootstrap = later


@pytest.mark.parametrize("field", ["scope_id", "capture_epoch", "source_system", "journal", "registry_authority"])
@pytest.mark.parametrize("bad", [None, True, 1, {}, "", " ", " padded", "bad\nvalue"])
def test_binding_rejects_invalid_strings(field, bad):
    with pytest.raises(ValueError):
        replace(BINDING, **{field: bad})


@pytest.mark.parametrize("bad", ["file:///state", "S3://registry-bucket/receiver-authority-v1",
                                 "s3://registry-bucket/epoch-a", "s3://registry-bucket/receiver-authority-v1/"])
def test_authority_requires_the_canonical_stable_namespace(bad):
    with pytest.raises(ValueError, match="authority"):
        replace(BINDING, registry_authority=bad)


@pytest.mark.parametrize("point", [None, POINT.to_dict(), replace(POINT, scope_id="other"),
                                  replace(POINT, capture_epoch="other"),
                                  replace(POINT, receiver_identity=replace(IDENTITY, journal_name="OTHER"))])
def test_binding_rejects_an_inconsistent_bootstrap(point):
    with pytest.raises(ValueError):
        replace(BINDING, bootstrap=point)


@pytest.mark.parametrize("corruption", ["extra", "missing", "version", "type", "bootstrap-extra"])
def test_binding_parser_rejects_unknown_or_malformed_records(corruption):
    record = BINDING.to_dict()
    if corruption == "extra":
        record["unexpected"] = "cannot-drop"
    elif corruption == "missing":
        del record["bootstrap"]
    elif corruption == "version":
        record["format_version"] = "future"
    elif corruption == "type":
        record["format_version"] = True
    else:
        record["bootstrap"]["unexpected"] = "cannot-drop"
    with pytest.raises(ValueError):
        CaptureBinding.from_dict(record)


@pytest.mark.parametrize("changes", [{"scope_id": "other"}, {"capture_epoch": "other"},
                                    {"receiver_identity": replace(IDENTITY, journal_name="OTHER")},
                                    {"receiver_identity": replace(IDENTITY, journal_library="OTHER")}])
def test_checkpoint_validation_rejects_another_scope_epoch_or_journal(changes):
    with pytest.raises(ValueError):
        BINDING.validate_checkpoint(replace(POINT, **changes))


def test_a_successor_checkpoint_keeps_the_bound_journal_and_epoch():
    successor = replace(IDENTITY, receiver_name="RCV2", receiver_library="OTHER", native_generation="generation-b")
    point = BINDING.checkpoint(successor, 1)
    BINDING.validate_checkpoint(point)
    assert point.scope_id == POINT.scope_id and point.capture_epoch == POINT.capture_epoch
    assert BINDING.bootstrap == POINT
