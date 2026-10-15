from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.qualification.verification_contract import (
    DATASETS,
    FORMAT_VERSION,
    Column,
    ReceiverWindow,
    VerificationScope,
)
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity


IDENTITY = ReceiverIdentity("LIB", "JRN", "LIB", "R9", "synthetic-v1", "generation-a")
BOOTSTRAP = BoundCheckpoint("scope", "epoch", IDENTITY, 10)
BINDING = CaptureBinding("scope", "epoch", "ibmi", "JRN",
                         "s3://synthetic-bucket/receiver-authority-v1", BOOTSTRAP)


def scope(**changes):
    fields = dict(
        pipeline_id="pipeline-1", table_id="table-1", copy_run_id="copy-1",
        source_library="LIB", source_table="ORDERS", destination_database="DEST",
        destination_schema="OUTPUT", history_table="ORDERS_HISTORY", mirror_table="ORDERS_MIRROR",
        columns=(Column("ID", "int"), Column("CODE", "text"), Column("AMOUNT", "decimal"),
                 Column("DAY", "date"), Column("STAMP", "timestamp")),
        primary_key=("ID", "CODE"), capture_binding=BINDING, copy_boundary=BOOTSTRAP,
        checkpoint=BINDING.checkpoint(IDENTITY, 20),
        receiver_windows=(ReceiverWindow(IDENTITY, 11, 20),), expected_snapshot_rows=1_000_000,
        producer_revision="a" * 40, producer_version="0.4.5",
    )
    fields.update(changes)
    return VerificationScope(**fields)


def rotated(count=3):
    identities = [IDENTITY] + [replace(IDENTITY, receiver_name=f"R{i:04d}", native_generation=f"g{i}")
                               for i in range(1, count)]
    windows = (ReceiverWindow(IDENTITY, 11, 20),) + tuple(
        ReceiverWindow(identity, 1, 5) for identity in identities[1:])
    return scope(checkpoint=BINDING.checkpoint(identities[-1], 5), receiver_windows=windows)


def test_strict_scope_round_trip_and_canonical_sha256():
    original = scope()
    record = original.to_dict()
    assert record["format_version"] == FORMAT_VERSION == "quadringent-native-verification-scope-v1"
    assert VerificationScope.from_dict(json.loads(json.dumps(record))) == original
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    assert original.sha256 == hashlib.sha256(canonical).hexdigest()
    assert VerificationScope.from_dict(dict(reversed(tuple(record.items())))).sha256 == original.sha256
    assert DATASETS == ("source", "snapshot", "journal", "positions", "raw", "history", "mirror")


def test_nested_contracts_are_closed_and_do_not_carry_verdicts():
    assert Column("ID", "int").to_dict() == {"name": "ID", "kind": "int"}
    window = ReceiverWindow(IDENTITY, 11, 20)
    assert window.to_dict() == {"identity": IDENTITY.to_dict(), "start_sequence": 11, "end_sequence": 20}
    assert ReceiverWindow.from_dict(window.to_dict()) == window
    assert not {"qualified", "provenance", "verdict", "observed_at", "framing"} & scope().to_dict().keys()


def test_dataclasses_and_serialized_copies_are_independent_and_immutable():
    original = scope()
    digest = original.sha256
    with pytest.raises(FrozenInstanceError):
        original.expected_snapshot_rows = 0
    with pytest.raises(FrozenInstanceError):
        original.columns[0].kind = "text"
    with pytest.raises(FrozenInstanceError):
        original.receiver_windows[0].end_sequence = 21
    exported = original.to_dict()
    exported["columns"][0]["kind"] = "text"
    exported["capture_binding"]["bootstrap"]["sequence"] = 0
    exported["receiver_windows"][0]["identity"]["native_generation"] = "changed"
    assert original.sha256 == digest


def test_composite_primary_key_and_all_five_types_are_preserved_in_order():
    original = scope()
    assert original.primary_key == ("ID", "CODE")
    assert [(c.name, c.kind) for c in original.columns] == [
        ("ID", "int"), ("CODE", "text"), ("AMOUNT", "decimal"), ("DAY", "date"), ("STAMP", "timestamp")]
    assert scope(columns=tuple(reversed(original.columns))).sha256 != original.sha256
    assert scope(primary_key=("CODE", "ID")).sha256 != original.sha256
    assert scope(columns=(Column("ID", "text"), *original.columns[1:])).sha256 != original.sha256


def test_zero_snapshot_and_empty_scan_at_the_copy_boundary_are_valid():
    empty = scope(expected_snapshot_rows=0, checkpoint=BOOTSTRAP,
                  receiver_windows=(ReceiverWindow(IDENTITY, 11, 10),))
    assert VerificationScope.from_dict(empty.to_dict()) == empty


def test_python_integer_sequences_have_no_sqlite_or_event_count_ceiling():
    large = 10 ** 100
    copy = BINDING.checkpoint(IDENTITY, large)
    original = scope(copy_boundary=copy, checkpoint=BINDING.checkpoint(IDENTITY, large + 1_000_001),
                     receiver_windows=(ReceiverWindow(IDENTITY, large + 1, large + 1_000_001),),
                     expected_snapshot_rows=large)
    assert VerificationScope.from_dict(original.to_dict()) == original


def test_receiver_order_is_explicit_and_resets_are_not_native_link_admission():
    original = rotated(4)
    assert [w.identity.receiver_name for w in original.receiver_windows] == ["R9", "R0001", "R0002", "R0003"]
    middle_swapped = replace(original, receiver_windows=(original.receiver_windows[0], original.receiver_windows[2],
                                                       original.receiver_windows[1], original.receiver_windows[3]))
    assert middle_swapped.sha256 != original.sha256
    assert original.receiver_windows[1].start_sequence == 1
    assert VerificationScope.from_dict(original.to_dict()).receiver_windows == original.receiver_windows


def test_five_hundred_windows_are_accepted_but_not_five_hundred_one():
    assert len(rotated(500).receiver_windows) == 500
    with pytest.raises(ValueError):
        rotated(501)


@pytest.mark.parametrize("kind", ["integer", "float", "binary", "INT", "", None, True, 1])
def test_column_kind_is_an_exact_supported_type(kind):
    with pytest.raises(ValueError):
        Column("ID", kind)


@pytest.mark.parametrize("name", ["", "1ID", "A.B", 'A"B', "A B", "A;DROP", " A", "A\n", "ÉTAT", "A" * 257, None, 7])
def test_column_rejects_non_identifiers_and_unbounded_names(name):
    with pytest.raises(ValueError):
        Column(name, "int")


@pytest.mark.parametrize("name", ["EVENT_ID", "event_id", "OpErAtIoN", "JOURNAL_RECEIVER", "journal_sequence"])
def test_business_column_cannot_overwrite_a_technical_event_field(name):
    with pytest.raises(ValueError):
        Column(name, "text")


@pytest.mark.parametrize("changes", [
    {"columns": ()}, {"columns": []}, {"columns": ("ID",)},
    {"columns": (Column("ID", "int"), Column("id", "text"))},
    {"primary_key": ()}, {"primary_key": []}, {"primary_key": ("ID", "id")},
    {"primary_key": ("MISSING",)}, {"primary_key": ("id",)}, {"primary_key": (True,)},
    {"receiver_windows": ()}, {"receiver_windows": []}, {"receiver_windows": ("R9",)},
    {"capture_binding": None}, {"copy_boundary": {}}, {"checkpoint": {}},
    {"expected_snapshot_rows": True}, {"expected_snapshot_rows": -1}, {"expected_snapshot_rows": 1.0},
    {"expected_snapshot_rows": "1000000"},
    {"producer_revision": "A" * 40}, {"producer_revision": "a" * 39},
    {"producer_revision": "g" * 40}, {"producer_revision": None},
    {"producer_version": ""}, {"producer_version": "0.4.5\n"}, {"producer_version": True},
    {"producer_version": "v" * 257}, {"mirror_table": "orders_history"},
])
def test_constructor_rejects_incomplete_mutable_or_ambiguous_scope(changes):
    with pytest.raises(ValueError):
        scope(**changes)


@pytest.mark.parametrize("field", ["pipeline_id", "table_id", "copy_run_id", "producer_version"])
@pytest.mark.parametrize("value", ["", " ", " leading", "trailing ", "a\x00b", "a\x7fb", "a\x85b", "a\u202eb", "a" * 257, None, True])
def test_opaque_identifiers_are_bounded_text_without_controls(field, value):
    with pytest.raises(ValueError):
        scope(**{field: value})


@pytest.mark.parametrize("field", ["source_library", "source_table", "destination_database", "destination_schema",
                                   "history_table", "mirror_table"])
@pytest.mark.parametrize("value", ["", "A.B", "A;B", " A", "A\n", "1A", "a" * 257, None, False])
def test_sql_scope_names_are_validated_before_use(field, value):
    with pytest.raises(ValueError):
        scope(**{field: value})


@pytest.mark.parametrize("start,end", [(True, 20), (11, False), (11.0, 20), (11, "20"),
                                       (0, 0), (-1, 0), (11, 9), (1, -1), (None, 20)])
def test_window_bounds_are_explicit_integers_or_one_empty_interval(start, end):
    with pytest.raises(ValueError):
        ReceiverWindow(IDENTITY, start, end)


@pytest.mark.parametrize("identity", [None, {}, "R9", True])
def test_window_requires_full_receiver_identity(identity):
    with pytest.raises(ValueError):
        ReceiverWindow(identity, 11, 20)


@pytest.mark.parametrize("fault", ["first_start", "last_end", "first_identity", "last_identity", "duplicate",
                                  "ambiguous_library", "reused_generation", "journal", "journal_library"])
def test_receiver_chain_refuses_foreign_missing_or_reused_identity(fault):
    original = rotated()
    windows = list(original.receiver_windows)
    if fault == "first_start":
        windows[0] = replace(windows[0], start_sequence=12)
    elif fault == "last_end":
        windows[-1] = replace(windows[-1], end_sequence=6)
    elif fault == "first_identity":
        windows[0] = replace(windows[0], identity=replace(IDENTITY, receiver_name="OTHER"))
    elif fault == "last_identity":
        windows[-1] = replace(windows[-1], identity=replace(windows[-1].identity, native_generation="other"))
    elif fault == "duplicate":
        windows.insert(1, windows[0])
    else:
        identity = windows[1].identity
        if fault == "ambiguous_library":
            identity = replace(IDENTITY, receiver_library="OTHER")
        elif fault == "reused_generation":
            identity = replace(IDENTITY, native_generation="other")
        else:
            identity = replace(identity, **{fault: "OTHER"} if fault == "journal_library" else {"journal_name": "OTHER"})
        windows[1] = replace(windows[1], identity=identity)
    with pytest.raises(ValueError):
        replace(original, receiver_windows=tuple(windows))


@pytest.mark.parametrize("field", ["scope_id", "capture_epoch"])
@pytest.mark.parametrize("point", ["copy_boundary", "checkpoint"])
def test_points_must_belong_to_the_original_binding(field, point):
    original = scope()
    with pytest.raises(ValueError):
        replace(original, **{point: replace(getattr(original, point), **{field: "foreign"})})


def test_copy_receiver_generation_cannot_reuse_the_original_bootstrap_name():
    reused = replace(IDENTITY, native_generation="replacement")
    with pytest.raises(ValueError):
        scope(copy_boundary=BINDING.checkpoint(reused, 10), checkpoint=BINDING.checkpoint(reused, 20),
              receiver_windows=(ReceiverWindow(reused, 11, 20),))


def test_foreign_source_system_is_not_a_native_ibmi_scope():
    with pytest.raises(ValueError):
        scope(capture_binding=replace(BINDING, source_system="other"))


@pytest.mark.parametrize("value", [None, [], "{}", True])
def test_from_dict_requires_an_object(value):
    with pytest.raises(ValueError):
        VerificationScope.from_dict(value)


@pytest.mark.parametrize("field", ["qualified", "provenance", "verdict", "observed_at", "framing", "ignored_columns"])
def test_unknown_top_level_fields_are_not_proof(field):
    record = scope().to_dict()
    record[field] = True
    with pytest.raises(ValueError):
        VerificationScope.from_dict(record)


@pytest.mark.parametrize("field", ["columns", "primary_key", "receiver_windows"])
def test_json_arrays_cannot_be_replaced_by_python_tuples(field):
    record = scope().to_dict()
    record[field] = tuple(record[field])
    with pytest.raises(ValueError):
        VerificationScope.from_dict(record)


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("format_version"),
    lambda d: d.update(format_version="quadringent-native-verification-scope-v2"),
    lambda d: d.pop("copy_run_id"),
    lambda d: d["columns"][0].update(ignored=True),
    lambda d: d["columns"][0].pop("kind"),
    lambda d: d["receiver_windows"][0].update(native_links_verified=True),
    lambda d: d["receiver_windows"][0].pop("end_sequence"),
    lambda d: d["checkpoint"].update(qualified=True),
    lambda d: d["capture_binding"].update(qualified=True),
    lambda d: d["receiver_windows"][0]["identity"].update(qualified=True),
])
def test_nested_schema_is_closed_and_original_input_is_not_rewritten(mutate):
    record = scope().to_dict()
    mutate(record)
    before = deepcopy(record)
    with pytest.raises(ValueError):
        VerificationScope.from_dict(record)
    assert record == before


def test_parsed_scope_retains_no_mutable_input_references():
    record = scope().to_dict()
    parsed = VerificationScope.from_dict(record)
    digest = parsed.sha256
    record["columns"][0]["kind"] = "text"
    record["primary_key"].reverse()
    record["capture_binding"]["bootstrap"]["sequence"] = 0
    record["receiver_windows"].clear()
    assert parsed == scope()
    assert parsed.sha256 == digest


def test_serialized_window_budget_is_checked_before_nested_conversion():
    record = rotated(500).to_dict()
    assert VerificationScope.from_dict(record) == rotated(500)
    record["receiver_windows"].append(None)
    with pytest.raises(ValueError, match="500"):
        VerificationScope.from_dict(record)


def test_copy_boundary_must_not_precede_its_same_receiver_bootstrap():
    with pytest.raises(ValueError):
        scope(copy_boundary=BINDING.checkpoint(IDENTITY, 9),
              receiver_windows=(ReceiverWindow(IDENTITY, 10, 20),))


def test_generation_change_in_the_binding_is_not_hidden_by_matching_windows():
    changed = replace(IDENTITY, native_generation="replacement")
    foreign = replace(BINDING, bootstrap=replace(BOOTSTRAP, receiver_identity=changed))
    with pytest.raises(ValueError):
        scope(capture_binding=foreign)
