"""Copy observations preserve originals without granting native admission."""

from copy import deepcopy
import base64
import hashlib
import json

import pytest

from quadringent.copy_observation import (
    ObservationReference,
    build_context,
    context_sha256,
    publish_copy_observations,
    read_copy_observations,
    validate_request,
)
from test_v2_capture_binding_handoff import boundary


def request():
    return {
        "format_version": "quadringent-copy-observation-request-v1",
        "mode": "fixed-single-member-format-v1",
        "source": {"library": "TESTLIB", "file": "ITEMS", "member": "ITEMS", "format": "ITEMSFMT"},
        "witness_window": {"receiver_library": "TESTLIB", "receiver_name": "RCV0001", "start": "1", "end": "4200"},
        "max_metadata_bytes": 1048576,
        "max_seconds": 60,
    }


def context():
    return build_context(
        pipeline_id="pipe-1", table_id="tbl-orders", run_id="run-1", boundary=boundary(), request=request()
    )


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def put_once(self, key, content):
        if key in self.objects and self.objects[key] != content:
            raise ValueError("collision")
        self.objects[key] = content
        self.writes.append(key)

    def get_bounded(self, key, max_bytes):
        if key not in self.objects:
            raise FileNotFoundError(key)
        value = self.objects[key]
        if len(value) > max_bytes:
            raise ValueError("oversize")
        return value


def test_request_is_explicit_bounded_and_not_a_native_admission():
    value = request()
    assert validate_request(value) == value
    assert context()["boundary"] == boundary().to_dict()
    assert len(context_sha256(context())) == 64
    value["mode"] = "native-verified"
    with pytest.raises(ValueError):
        validate_request(value)


@pytest.mark.parametrize("damage", ["extra", "bool", "decimal", "empty", "sequence", "range", "limit", "source"])
def test_request_rejects_ambiguous_or_unbounded_values(damage):
    value = request()
    if damage == "extra":
        value["history_verified"] = True
    elif damage == "bool":
        value["max_seconds"] = True
    elif damage == "decimal":
        value["witness_window"]["start"] = 1.5
    elif damage == "empty":
        value["source"]["member"] = ""
    elif damage == "sequence":
        value["witness_window"]["end"] = "1" * 22
    elif damage == "range":
        value["witness_window"]["end"] = "0"
    elif damage == "limit":
        value["max_metadata_bytes"] = 1048577
    else:
        value["source"]["library"] = "../OTHER"
    with pytest.raises(ValueError):
        validate_request(value)


def test_context_preserves_original_frontier_and_requires_bound_identity():
    value = context()
    other = deepcopy(value)
    other["boundary"]["bound_checkpoint"]["sequence"] = 4201
    assert context_sha256(other) != context_sha256(value)
    with pytest.raises(ValueError):
        build_context(pipeline_id="pipe-1", table_id="tbl-orders", run_id="run-1", boundary=None, request=request())


def test_current_witness_window_is_explicit_without_rewriting_original_context():
    from quadringent.copy_observation import validate_observation

    current = context()
    document = observation(current=current)
    original = encode(current)
    document["witness_window"]["end_sequence"] = "4205"
    pinned = encode(document)
    with pytest.raises(ValueError, match="witness_window_mismatch"):
        validate_observation(document, context=current, phase="before")
    window = dict(current["request"]["witness_window"], end="4205")
    assert validate_observation(document, context=current, phase="before", witness_window=window) == document
    assert encode(current) == original and encode(document) == pinned
    assert document["context_sha256"] == context_sha256(current)


def test_explicit_witness_window_does_not_bypass_window_validation():
    from quadringent.copy_observation import validate_observation

    current = context()
    with pytest.raises(ValueError, match="invalid_witness_window"):
        validate_observation(observation(current=current), context=current, phase="before",
                             witness_window=dict(current["request"]["witness_window"], start="0"))


@pytest.mark.parametrize("bad", [True, 0, -1, 1048577])
def test_reference_has_strict_positive_bounded_bytes(bad):
    with pytest.raises(ValueError):
        ObservationReference("copy-observations-v1/" + "a" * 64 + "/manifest.json", "b" * 64, bad)


def test_reference_namespace_cannot_escape_or_alias():
    for key in ("../manifest.json", "copy-observations-v1/../manifest.json", "copy-observations-v1/a.json"):
        with pytest.raises(ValueError):
            ObservationReference(key, "b" * 64, 1)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def test_publication_rejects_invalid_observations_before_any_write():
    store = Store()
    with pytest.raises(ValueError):
        publish_copy_observations(
            store,
            context=context(),
            before_bytes=b"{}",
            after_bytes=b"{}",
            snapshot_inventory=[],
            rows_copied=0,
            evidence_key="proof",
        )
    assert not store.writes


def test_missing_manifest_never_becomes_an_eligible_old_copy():
    reference = ObservationReference("copy-observations-v1/" + "a" * 64 + "/manifest.json", "b" * 64, 1)
    with pytest.raises(ValueError):
        read_copy_observations(Store(), reference)


def observation(phase="before", current=None):
    value = {
        "format_version": "quadringent-native-copy-observation-v2",
        "phase": "before",
        "context_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "copy_run_id": "12345678-1234-4234-8234-123456789abc",
        "capture_binding_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "copy_boundary_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "copy_boundary": {"receiver_library": "TESTLIB", "receiver_name": "RCV1", "sequence": "20"},
        "journal": {"library": "TESTLIB", "name": "JRN"},
        "witness_window": {
            "receiver_library": "TESTLIB",
            "receiver_name": "RCV1",
            "start_sequence": "1",
            "end_sequence": "20",
        },
        "source_library": "TESTLIB",
        "source_file": "ITEMS",
        "source_member": "ITEMS",
        "format_name": "ITEMSFMT",
        "started_at": "2026-10-16T08:00:00Z",
        "completed_at": "2026-10-16T08:00:01Z",
        "admission": "not_evaluated",
        "jdbc_job": {"name": "QZDASOINIT", "user": "TESTUSR", "number": "123457"},
        "format": {
            "level_id": "ABC1234567890",
            "record_length": 32,
            "layout_sha256": "5fc407dabfcfe66b0c3e31f67e01a91c4630f6b3db881cf968493e1ed4bded8a",
            "fields": [
                {
                    "system_name": "ID",
                    "sql_name": "ID",
                    "kind": "int",
                    "native_type": "0000",
                    "input_offset": 0,
                    "output_offset": 0,
                    "byte_length": 4,
                    "digits": 10,
                    "scale": 0,
                    "ccsid": 0,
                    "allocated_length": 0,
                },
                {
                    "system_name": "AMOUNT",
                    "sql_name": "AMOUNT",
                    "kind": "decimal",
                    "native_type": "0003",
                    "input_offset": 4,
                    "output_offset": 4,
                    "byte_length": 20,
                    "digits": 38,
                    "scale": 12,
                    "ccsid": 0,
                    "allocated_length": 0,
                },
                {
                    "system_name": "LABEL",
                    "sql_name": "LONG_BUSINESS_LABEL",
                    "kind": "text",
                    "native_type": "0004",
                    "input_offset": 24,
                    "output_offset": 24,
                    "byte_length": 8,
                    "digits": 0,
                    "scale": 0,
                    "ccsid": 273,
                    "allocated_length": 0,
                },
            ],
            "original_id": "format",
        },
        "rcac": {
            "row_permissions_state": " ",
            "column_masks_state": " ",
            "row_permission_count": 0,
            "column_mask_count": 0,
            "original_id": "rcac",
            "basis": "no_controls_defined",
        },
        "member": {
            "jid_hex": "00112233445566778899",
            "witnesses": [
                {
                    "receiver_library": "TESTLIB",
                    "receiver_name": "RCV1",
                    "sequence": "20",
                    "journal_code": "R",
                    "journal_entry_type": "PT",
                    "object": "ITEMS     TESTLIB   ITEMS     ",
                    "object_type": "*FILE",
                },
                {
                    "receiver_library": "TESTLIB",
                    "receiver_name": "RCV1",
                    "sequence": "20",
                    "journal_code": "R",
                    "journal_entry_type": "PT",
                    "object": "ITEMS     TESTLIB   ITEMS     ",
                    "object_type": "*FILE",
                },
            ],
            "original_ids": ["member-before", "jid-first", "jid-last", "member-after"],
        },
        "originals": [
            {
                "id": "file",
                "program": "QDBRTVFD",
                "format": "FILD0100",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAAAMQAAADECAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ==",
                "receiver_sha256": "472c779fdb4486d9596bd1da10a9e4857d932d3d067e90c806a078485ca5c34d",
                "receiver_bytes": 49,
                "qualified_target_base64": "yePF1OJAQEBAQOPF4uPTycJAQEA=",
                "qualified_target_sha256": "b9f42c0408a6cabe65bc182ce46e17c3fa7167f5442b905e9251ec02a35fda52",
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "format",
                "program": "QDBRTVFD",
                "format": "FILD0200",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAACUAAAAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIMnjxdTixtTjQEDBwsPx8vP09fb3+PnwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHDJxEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEDJxEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEAAAAMAAAAAAAAAAAAEAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwwdTW5NXjQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAwdTW5NXjQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAAAMDAAAABAAAAAQAFAAmAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcNPBwsXTQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQNPBwsXTQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQAAEAwAAABgAAAAYAAgAAAAAAAAAAAAAAAAAAAAAAAABEQAAAAAAAAAAAAAAAAAAAA==",
                "receiver_sha256": "d849f579e56b7c085f8a3c366dfecc9809c5fa83ac3b29eeb8653cca9e150453",
                "receiver_bytes": 592,
                "qualified_target_base64": "yePF1OJAQEBAQOPF4uPTycJAQEA=",
                "qualified_target_sha256": "b9f42c0408a6cabe65bc182ce46e17c3fa7167f5442b905e9251ec02a35fda52",
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "rcac",
                "program": "QDBRTVFD",
                "format": "FILD0500",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAAAcAAAAHAAAAAAAAAAAAAAAAAAQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                "receiver_sha256": "e422382e9dae079611109c6c01d37d20cda04f4180d97a45e3bba62e95b8f74b",
                "receiver_bytes": 112,
                "qualified_target_base64": "yePF1OJAQEBAQOPF4uPTycJAQEA=",
                "qualified_target_sha256": "b9f42c0408a6cabe65bc182ce46e17c3fa7167f5442b905e9251ec02a35fda52",
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "member-before",
                "program": "QUSRMBRD",
                "format": "MBRD0200",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAABpgAAAabJ48XU4kBAQEBA48Xi49PJwkBAQMnjxdTiQEBAQEDXxkBAQEBAQEBAAAAAAAAAAAAAAPHy9vDx8PHw8PDw8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADw8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQEBAQEBAQEBAQEBAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQoAAACcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "receiver_sha256": "a59de1d46996bafc42f9c12808820a91224e34274c5509dc40cd2bd75b0f7b5b",
                "receiver_bytes": 422,
                "qualified_target_base64": None,
                "qualified_target_sha256": None,
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "jid-first",
                "program": "QJORJIDI",
                "format": "RJID0100",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAAAXgAAAF7R2dVAQEBAQEBA48Xi49PJwkBAQAARIjNEVWZ3iJnJ48XU4kBAQEBA48Xi49PJwkBAQMnjxdTiQEBAQEBc2MTE4kBAQEBAQEBAQEBAQEBAQEBAQEBAQA==",
                "receiver_sha256": "64910043238e1d4d00aab124dd2189a9f96e8e5add706610726e70c1c3bcd4b2",
                "receiver_bytes": 94,
                "qualified_target_base64": None,
                "qualified_target_sha256": None,
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "jid-last",
                "program": "QJORJIDI",
                "format": "RJID0100",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAAAXgAAAF7R2dVAQEBAQEBA48Xi49PJwkBAQAARIjNEVWZ3iJnJ48XU4kBAQEBA48Xi49PJwkBAQMnjxdTiQEBAQEBc2MTE4kBAQEBAQEBAQEBAQEBAQEBAQEBAQA==",
                "receiver_sha256": "64910043238e1d4d00aab124dd2189a9f96e8e5add706610726e70c1c3bcd4b2",
                "receiver_bytes": 94,
                "qualified_target_base64": None,
                "qualified_target_sha256": None,
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
            {
                "id": "member-after",
                "program": "QUSRMBRD",
                "format": "MBRD0200",
                "ccsid": 273,
                "job_before": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "job_after": {"name": "QZRCSRVS", "user": "TESTUSR", "number": "123456", "ccsid": 273},
                "receiver_base64": "AAABpgAAAabJ48XU4kBAQEBA48Xi49PJwkBAQMnjxdTiQEBAQEDXxkBAQEBAQEBAAAAAAAAAAAAAAPHy9vDx8PHw8PDw8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADw8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQEBAQEBAQEBAQEBAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQoAAACcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "receiver_sha256": "a59de1d46996bafc42f9c12808820a91224e34274c5509dc40cd2bd75b0f7b5b",
                "receiver_bytes": 422,
                "qualified_target_base64": None,
                "qualified_target_sha256": None,
                "error_base64": "AAAAEAAAAAAAAAAAAAAAAA==",
                "error_sha256": "c20b8d84748bc316bc183e115720b53abb530f7ced5debb8928f835c8efd1b54",
            },
        ],
        "file": {"original_id": "file", "flags_8": 2, "flags_23": 0, "access_control_defined": False},
    }
    current = context() if current is None else current
    from quadringent_control_plane.v2.executor.boundary import JournalBoundary

    point = JournalBoundary.from_dict(current["boundary"]).require_bound()
    value.update(
        phase=phase,
        context_sha256=context_sha256(current),
        copy_run_id=current["run_id"],
        capture_binding_sha256=context_sha256(current["boundary"]["capture_binding"]),
        copy_boundary_sha256=context_sha256(current["boundary"]),
    )
    value["copy_boundary"] = {
        "receiver_library": point.receiver_identity.receiver_library,
        "receiver_name": point.receiver_identity.receiver_name,
        "sequence": str(point.sequence),
    }
    value["journal"] = {
        "library": point.receiver_identity.journal_library,
        "name": point.receiver_identity.journal_name,
    }
    window = current["request"]["witness_window"]
    value["witness_window"] = {
        "receiver_library": window["receiver_library"],
        "receiver_name": window["receiver_name"],
        "start_sequence": window["start"],
        "end_sequence": window["end"],
    }
    for field, key in (
        ("source_library", "library"),
        ("source_file", "file"),
        ("source_member", "member"),
        ("format_name", "format"),
    ):
        value[field] = current["request"]["source"][key]
    for witness in value["member"]["witnesses"]:
        witness.update(
            receiver_library=window["receiver_library"],
            receiver_name=window["receiver_name"],
            sequence=window["end"],
            object="".join(current["request"]["source"][k].ljust(10) for k in ("file", "library", "member")),
        )
    if phase == "after":
        value["started_at"] = "2026-10-16T08:02:00Z"
        value["completed_at"] = "2026-10-16T08:02:01Z"
    return value


def published():
    store = Store()
    manifest = publish_copy_observations(
        store,
        context=context(),
        before_bytes=encode(observation()),
        after_bytes=encode(observation("after")),
        snapshot_inventory=[],
        rows_copied=0,
        evidence_key="proof",
    )
    return store, manifest


def test_real_observation_fixture_roundtrips_original_bytes_and_manifest():
    store, reference = published()
    manifest = read_copy_observations(store, reference, expected_context=context(), expected_batches=[])
    assert manifest["admission"] == "not_evaluated"
    assert manifest["context"] == context()
    assert len(store.objects) == 3
    assert store.objects[manifest["before"]["key"]] == encode(observation())
    assert store.objects[manifest["after"]["key"]] == encode(observation("after"))


@pytest.mark.parametrize(
    "damage",
    [
        "context",
        "run",
        "frontier",
        "binding",
        "source",
        "phase",
        "original",
        "bytes",
        "error",
        "job",
        "ccsid",
        "field",
        "jid",
        "rcac",
        "missing",
        "extra",
    ],
)
def test_observation_context_originals_and_metadata_are_checked_before_write(damage):
    before = observation()
    if damage == "context":
        before["context_sha256"] = "0" * 64
    elif damage == "run":
        before["copy_run_id"] = "other"
    elif damage == "frontier":
        before["copy_boundary"]["sequence"] = "4201"
    elif damage == "binding":
        before["capture_binding_sha256"] = "0" * 64
    elif damage == "source":
        before["source_member"] = "OTHER"
    elif damage == "phase":
        before["phase"] = "after"
    elif damage == "original":
        before["originals"][0]["receiver_sha256"] = "0" * 64
    elif damage == "bytes":
        before["originals"][0]["receiver_bytes"] += 1
    elif damage == "error":
        before["originals"][0]["error_sha256"] = "0" * 64
    elif damage == "job":
        before["originals"][0]["job_after"]["number"] = "999999"
    elif damage == "ccsid":
        before["originals"][0]["ccsid"] = 65535
    elif damage == "field":
        before["format"]["fields"][0]["byte_length"] = -1
    elif damage == "jid":
        before["member"]["jid_hex"] = "00"
    elif damage == "rcac":
        before["rcac"]["row_permissions_state"] = "A"
    elif damage == "missing":
        before["originals"].pop()
    else:
        before["history_verified"] = True
    store = Store()
    with pytest.raises(ValueError):
        publish_copy_observations(
            store,
            context=context(),
            before_bytes=encode(before),
            after_bytes=encode(observation("after")),
            snapshot_inventory=[],
            rows_copied=0,
            evidence_key="proof",
        )
    assert store.writes == []


@pytest.mark.parametrize("damage", ["after", "missing", "tamper", "unknown", "frontier"])
def test_sealed_manifest_dependencies_are_revalidated(damage):
    store, reference = published()
    manifest = read_copy_observations(store, reference)
    if damage == "after":
        store.objects[manifest["after"]["key"]] = encode(observation())
    elif damage == "missing":
        del store.objects[manifest["before"]["key"]]
    elif damage == "tamper":
        store.objects[manifest["before"]["key"]] += b" "
    else:
        if damage == "unknown":
            manifest["native_admission"] = True
        else:
            manifest["context"]["boundary"]["bound_checkpoint"]["sequence"] += 1
        payload = encode(manifest)
        store.objects[reference.key] = payload
        reference = ObservationReference(reference.key, digest(payload), len(payload))
    with pytest.raises(ValueError):
        read_copy_observations(store, reference, expected_context=context())


@pytest.mark.parametrize("change", ["level", "jid", "order", "file"])
def test_before_after_changes_or_time_reversal_never_publish(change):
    after = observation("after")
    if change == "level":
        after["format"]["level_id"] = "XYZ1234567890"
    elif change == "jid":
        after["member"]["jid_hex"] = "ffeeddccbbaa99887766"
    elif change == "file":
        after["file"]["flags_8"] = 3
        replace_receiver(after, "file", lambda raw: raw.__setitem__(8, 3))
        update_layout_hash(after)
    else:
        after["started_at"] = "2026-10-16T07:59:00Z"
        after["completed_at"] = "2026-10-16T07:59:01Z"
    store = Store()
    with pytest.raises(ValueError):
        publish_copy_observations(
            store,
            context=context(),
            before_bytes=encode(observation()),
            after_bytes=encode(after),
            snapshot_inventory=[],
            rows_copied=0,
            evidence_key="proof",
        )
    assert store.writes == []


def test_nanosecond_clock_reversal_is_not_hidden_by_datetime_microseconds():
    before, after = observation(), observation("after")
    before["completed_at"] = "2026-10-16T08:00:01.000000009Z"
    after["started_at"] = "2026-10-16T08:00:01.000000001Z"
    after["completed_at"] = "2026-10-16T08:00:02Z"
    with pytest.raises(ValueError):
        publish_copy_observations(
            Store(),
            context=context(),
            before_bytes=encode(before),
            after_bytes=encode(after),
            snapshot_inventory=[],
            rows_copied=0,
            evidence_key="proof",
        )


def test_v1_observation_without_file_original_cannot_feed_new_copy():
    from quadringent.copy_observation import validate_observation

    value = observation()
    value["format_version"] = "quadringent-native-copy-observation-v1"
    value.pop("file", None)
    value["rcac"].pop("basis", None)
    value["originals"] = [item for item in value["originals"] if item["id"] != "file"]
    with pytest.raises(ValueError):
        validate_observation(value, context=context(), phase="before")


def replace_receiver(value, original_id, update):
    original = next(item for item in value["originals"] if item["id"] == original_id)
    raw = bytearray(base64.b64decode(original["receiver_base64"]))
    update(raw)
    original["receiver_base64"] = base64.b64encode(raw).decode()
    original["receiver_sha256"] = digest(raw)


@pytest.mark.parametrize("states", [(" ", " "), ("I", "I")])
def test_no_controls_retains_exact_blank_or_inactive_states(states):
    from quadringent.copy_observation import validate_observation

    value = observation()
    value["rcac"]["row_permissions_state"], value["rcac"]["column_masks_state"] = states
    replace_receiver(value, "rcac", lambda raw: raw.__setitem__(slice(21, 23), "".join(states).encode("cp273")))
    update_layout_hash(value)
    parsed = validate_observation(value, context=context(), phase="before")
    assert (parsed["rcac"]["row_permissions_state"], parsed["rcac"]["column_masks_state"]) == states


def update_layout_hash(value):
    data = bytearray()
    ccsids = bytearray()
    for original in value["originals"][:3]:
        for key in ("receiver_base64", "qualified_target_base64", "error_base64"):
            data.extend(base64.b64decode(original[key]))
        ccsids.extend(original["ccsid"].to_bytes(4, "big"))
    value["format"]["layout_sha256"] = digest(data + ccsids)


@pytest.mark.parametrize("damage", ["active-bit", "boolean", "negative", "overflow", "raw-mismatch", "missing-file"])
def test_file_control_basis_is_exact_bounded_and_backed_by_native_bytes(damage):
    from quadringent.copy_observation import validate_observation

    value = observation()
    if damage == "active-bit":
        value["file"]["flags_23"] = 1
        replace_receiver(value, "file", lambda raw: raw.__setitem__(23, 1))
        update_layout_hash(value)
    elif damage == "boolean":
        value["file"]["flags_8"] = True
    elif damage == "negative":
        value["file"]["flags_23"] = -1
    elif damage == "overflow":
        value["file"]["flags_23"] = 256
    elif damage == "raw-mismatch":
        value["file"]["flags_8"] = 3
    else:
        value["originals"].pop(0)
    with pytest.raises(ValueError):
        validate_observation(value, context=context(), phase="before")


@pytest.mark.parametrize("damage", ["mixed", "trimmed", "basis", "count", "raw-state", "raw-count", "layout-hash"])
def test_no_controls_cannot_be_inferred_from_unbacked_or_normalized_metadata(damage):
    from quadringent.copy_observation import validate_observation

    value = observation()
    if damage == "mixed":
        value["rcac"]["column_masks_state"] = "I"
    elif damage == "trimmed":
        value["rcac"]["row_permissions_state"] = ""
    elif damage == "basis":
        value["rcac"]["basis"] = "inactive"
    elif damage == "count":
        value["rcac"]["row_permission_count"] = True
    elif damage == "raw-state":
        replace_receiver(value, "rcac", lambda raw: raw.__setitem__(21, "A".encode("cp273")[0]))
        update_layout_hash(value)
    elif damage == "raw-count":
        replace_receiver(value, "rcac", lambda raw: raw.__setitem__(24, 1))
        update_layout_hash(value)
    else:
        value["format"]["layout_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_observation(value, context=context(), phase="before")


@pytest.mark.parametrize("damage", ["zero", "two", "negative", "truncated", "max-members", "logical", "source"])
def test_fixed_single_member_requires_actual_native_count_and_physical_data_file(damage):
    from quadringent.copy_observation import validate_observation

    value = observation()
    original = value["originals"][0]
    raw = bytearray(base64.b64decode(original["receiver_base64"]))
    raw.extend(bytes(max(0, 49 - len(raw))))
    raw[47:49] = (1).to_bytes(2, "big", signed=True)
    if damage == "truncated":
        raw = raw[:48]
    elif damage == "logical":
        raw[8] |= 0x20
    elif damage == "source":
        raw[8] |= 0x08
    else:
        raw[47:49] = {"zero": 0, "two": 2, "negative": -1, "max-members": 2}[damage].to_bytes(2, "big", signed=True)
        if damage == "max-members":
            raw[41:43] = (1).to_bytes(2, "big", signed=True)
    raw[:4] = raw[4:8] = len(raw).to_bytes(4, "big")
    original["receiver_base64"] = base64.b64encode(raw).decode()
    original["receiver_sha256"] = digest(raw)
    original["receiver_bytes"] = len(raw)
    value["file"]["flags_8"] = raw[8]
    update_layout_hash(value)
    with pytest.raises(ValueError):
        validate_observation(value, context=context(), phase="before")
