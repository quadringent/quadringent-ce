"""Bind new-copy native observations to originals; observation is not admission."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re

from .checkpoint import _decode_checkpoint_json
from .object_store import _validate_object_key


REQUEST_VERSION = "quadringent-copy-observation-request-v1"
OBSERVATION_VERSION = "quadringent-native-copy-observation-v2"
MANIFEST_VERSION = "quadringent-copy-observations-v1"
MAX_OBSERVATION_BYTES = 1024 * 1024
_NAME = re.compile(r"[A-Z#$@][A-Z0-9_$#@]{0,9}")
_SHA = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"copy-observations-v1/([0-9a-f]{64})/(before|after|manifest)\.json")


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("copy_observation_invalid_fields")


def _positive(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("copy_observation_invalid_limit")


def _name(value):
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise ValueError("copy_observation_invalid_native_name")


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("copy_observation_invalid_digest")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode() + b"\n"


def context_sha256(context):
    return hashlib.sha256(encode(context)).hexdigest()


def validate_request(request):
    _fields(request, "format_version mode source witness_window max_metadata_bytes max_seconds")
    if request["format_version"] != REQUEST_VERSION or request["mode"] != "fixed-single-member-format-v1":
        raise ValueError("copy_observation_unsupported_mode")
    _fields(request["source"], "library file member format")
    for value in request["source"].values():
        _name(value)
    window = request["witness_window"]
    _fields(window, "receiver_library receiver_name start end")
    _name(window["receiver_library"])
    _name(window["receiver_name"])
    for key in ("start", "end"):
        if type(window[key]) is not str or re.fullmatch(r"[1-9][0-9]{0,20}", window[key]) is None:
            raise ValueError("copy_observation_invalid_witness_window")
    if int(window["end"]) < int(window["start"]):
        raise ValueError("copy_observation_invalid_witness_window")
    _positive(request["max_metadata_bytes"], MAX_OBSERVATION_BYTES)
    _positive(request["max_seconds"], 60)
    return deepcopy(request)


def build_context(*, pipeline_id, table_id, run_id, boundary, request):
    from quadringent_control_plane.v2.executor.boundary import JournalBoundary

    for value in (pipeline_id, table_id, run_id):
        if type(value) is not str or not value or len(value) > 256 or value.strip() != value:
            raise ValueError("copy_observation_invalid_identity")
    if type(boundary) is not JournalBoundary:
        raise ValueError("copy_observation_bound_frontier_required")
    boundary.require_bound()
    return {
        "pipeline_id": pipeline_id,
        "table_id": table_id,
        "run_id": run_id,
        "boundary": boundary.to_dict(),
        "request": validate_request(request),
    }


def _context(context):
    from quadringent_control_plane.v2.executor.boundary import JournalBoundary

    _fields(context, "pipeline_id table_id run_id boundary request")
    expected = build_context(
        pipeline_id=context["pipeline_id"],
        table_id=context["table_id"],
        run_id=context["run_id"],
        boundary=JournalBoundary.from_dict(context["boundary"]),
        request=context["request"],
    )
    if expected != context:
        raise ValueError("copy_observation_context_mismatch")
    return expected


@dataclass(frozen=True)
class ObservationReference:
    key: str
    sha256: str
    byte_count: int

    def __post_init__(self):
        if type(self.key) is not str or _KEY.fullmatch(self.key) is None:
            raise ValueError("copy_observation_invalid_reference")
        _sha(self.sha256)
        _positive(self.byte_count, MAX_OBSERVATION_BYTES)

    def to_dict(self):
        return {"key": self.key, "sha256": self.sha256, "byte_count": self.byte_count}

    @classmethod
    def from_dict(cls, value):
        _fields(value, "key sha256 byte_count")
        return cls(**value)


def _read(store, reference):
    try:
        data = store.get_bounded(reference.key, reference.byte_count)
        if (
            type(data) is not bytes
            or len(data) != reference.byte_count
            or hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise ValueError("copy_observation_original_mismatch")
        value = _decode_checkpoint_json(data)
        if type(value) is not dict:
            raise ValueError("copy_observation_original_invalid")
        return value, data
    except (ValueError, FileNotFoundError, TypeError):
        raise ValueError("copy_observation_original_unavailable") from None


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("copy_observation_invalid_timestamp")
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(?:Z|\+00:00)", value
    )
    if match is None:
        raise ValueError("copy_observation_invalid_timestamp")
    try:
        seconds = datetime.fromisoformat(match[1] + "+00:00")
    except ValueError:
        raise ValueError("copy_observation_invalid_timestamp") from None
    return seconds, Decimal("0." + (match[2] or "0"))


def _job(value, *, ccsid=False):
    _fields(value, "name user number" + (" ccsid" if ccsid else ""))
    _name(value["name"])
    _name(value["user"])
    if type(value["number"]) is not str or re.fullmatch(r"[0-9]{6}", value["number"]) is None:
        raise ValueError("copy_observation_invalid_job")
    if ccsid and (type(value["ccsid"]) is not int or value["ccsid"] not in {37, 273, 367}):
        raise ValueError("copy_observation_unsupported_ccsid")


def _binary(encoded, digest, *, maximum, length=None):
    _sha(digest)
    if type(encoded) is not str or len(encoded) > ((maximum + 2) // 3) * 4:
        raise ValueError("copy_observation_invalid_original")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("copy_observation_invalid_original") from None
    if (
        not 1 <= len(data) <= maximum
        or length is not None
        and len(data) != length
        or hashlib.sha256(data).hexdigest() != digest
    ):
        raise ValueError("copy_observation_original_mismatch")
    return data


def validate_observation(document, *, context, phase):
    _context(context)
    _fields(
        document,
        "format_version phase context_sha256 copy_run_id capture_binding_sha256 "
        "copy_boundary_sha256 copy_boundary journal witness_window source_library source_file "
        "source_member format_name started_at completed_at admission jdbc_job file format rcac member originals",
    )
    if (
        document["format_version"] != OBSERVATION_VERSION
        or phase not in {"before", "after"}
        or document["phase"] != phase
        or document["context_sha256"] != context_sha256(context)
        or document["copy_run_id"] != context["run_id"]
        or document["admission"] != "not_evaluated"
        or document["capture_binding_sha256"] != context_sha256(context["boundary"]["capture_binding"])
        or document["copy_boundary_sha256"] != context_sha256(context["boundary"])
    ):
        raise ValueError("copy_observation_context_mismatch")
    boundary = context["boundary"]
    point = boundary["bound_checkpoint"]
    identity = point["receiver_identity"]
    if document["copy_boundary"] != {
        "receiver_library": identity["receiver_library"],
        "receiver_name": identity["receiver_name"],
        "sequence": str(point["sequence"]),
    }:
        raise ValueError("copy_observation_frontier_mismatch")
    if document["journal"] != {"library": identity["journal_library"], "name": identity["journal_name"]}:
        raise ValueError("copy_observation_journal_mismatch")
    request = context["request"]
    for field, key in (
        ("source_library", "library"),
        ("source_file", "file"),
        ("source_member", "member"),
        ("format_name", "format"),
    ):
        if document[field] != request["source"][key]:
            raise ValueError("copy_observation_source_mismatch")
    window = request["witness_window"]
    if document["witness_window"] != {
        "receiver_library": window["receiver_library"],
        "receiver_name": window["receiver_name"],
        "start_sequence": window["start"],
        "end_sequence": window["end"],
    }:
        raise ValueError("copy_observation_witness_window_mismatch")
    if _timestamp(document["completed_at"]) < _timestamp(document["started_at"]):
        raise ValueError("copy_observation_invalid_time_order")
    _job(document["jdbc_job"])
    layout = document["format"]
    _fields(layout, "level_id record_length layout_sha256 fields original_id")
    if (
        type(layout["level_id"]) is not str
        or re.fullmatch(r"[!-~]{13}", layout["level_id"]) is None
        or layout["original_id"] != "format"
    ):
        raise ValueError("copy_observation_invalid_format")
    _positive(layout["record_length"], MAX_OBSERVATION_BYTES)
    _sha(layout["layout_sha256"])
    if type(layout["fields"]) is not list or not 1 <= len(layout["fields"]) <= 2048:
        raise ValueError("copy_observation_invalid_fields")
    names = set()
    for field in layout["fields"]:
        _fields(
            field,
            "system_name sql_name kind native_type input_offset output_offset byte_length digits scale ccsid allocated_length",
        )
        _name(field["system_name"])
        if (
            type(field["sql_name"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_$#@]{1,128}", field["sql_name"]) is None
            or field["sql_name"] in names
            or field["kind"] not in {"int", "decimal", "text"}
            or type(field["native_type"]) is not str
            or re.fullmatch(r"[0-9A-Fa-f]{4}", field["native_type"]) is None
        ):
            raise ValueError("copy_observation_invalid_field")
        names.add(field["sql_name"])
        for key in ("input_offset", "output_offset", "byte_length", "digits", "scale", "ccsid", "allocated_length"):
            if type(field[key]) is not int or not 0 <= field[key] <= MAX_OBSERVATION_BYTES:
                raise ValueError("copy_observation_invalid_field")
        if field["byte_length"] < 1 or field["scale"] > field["digits"]:
            raise ValueError("copy_observation_invalid_field")
    file = document["file"]
    _fields(file, "original_id flags_8 flags_23 access_control_defined")
    if (
        file["original_id"] != "file"
        or type(file["access_control_defined"]) is not bool
        or file["access_control_defined"]
        or any(type(file[k]) is not int or not 0 <= file[k] <= 255 for k in ("flags_8", "flags_23"))
        or file["flags_23"] & 1
    ):
        raise ValueError("copy_observation_unsupported_access_controls")
    rcac = document["rcac"]
    _fields(rcac, "row_permissions_state column_masks_state row_permission_count column_mask_count original_id basis")
    if (
        rcac["basis"] != "no_controls_defined"
        or rcac["original_id"] != "rcac"
        or (rcac["row_permissions_state"], rcac["column_masks_state"]) not in {(" ", " "), ("I", "I")}
        or any(type(rcac[k]) is not int or rcac[k] != 0 for k in ("row_permission_count", "column_mask_count"))
    ):
        raise ValueError("copy_observation_unsupported_rcac")
    member = document["member"]
    _fields(member, "jid_hex witnesses original_ids")
    if (
        type(member["jid_hex"]) is not str
        or re.fullmatch(r"[0-9a-f]{20}", member["jid_hex"]) is None
        or member["original_ids"] != ["member-before", "jid-first", "jid-last", "member-after"]
        or type(member["witnesses"]) is not list
        or len(member["witnesses"]) != 2
    ):
        raise ValueError("copy_observation_invalid_member")
    for witness in member["witnesses"]:
        _fields(witness, "receiver_library receiver_name sequence journal_code journal_entry_type object object_type")
        if (
            witness["receiver_library"] != window["receiver_library"]
            or witness["receiver_name"] != window["receiver_name"]
            or type(witness["sequence"]) is not str
            or re.fullmatch(r"[1-9][0-9]{0,20}", witness["sequence"]) is None
            or not int(window["start"]) <= int(witness["sequence"]) <= int(window["end"])
            or (witness["journal_code"], witness["journal_entry_type"])
            not in {("R", "PT"), ("R", "PX"), ("R", "UP"), ("R", "UB"), ("R", "DL"), ("F", "JM")}
            or witness["object_type"] != "*FILE"
            or witness["object"] != "".join(request["source"][k].ljust(10) for k in ("file", "library", "member"))
        ):
            raise ValueError("copy_observation_invalid_witness")
    originals = document["originals"]
    expected = [
        ("file", "QDBRTVFD", "FILD0100"),
        ("format", "QDBRTVFD", "FILD0200"),
        ("rcac", "QDBRTVFD", "FILD0500"),
        ("member-before", "QUSRMBRD", "MBRD0200"),
        ("jid-first", "QJORJIDI", "RJID0100"),
        ("jid-last", "QJORJIDI", "RJID0100"),
        ("member-after", "QUSRMBRD", "MBRD0200"),
    ]
    if type(originals) is not list or len(originals) != len(expected):
        raise ValueError("copy_observation_originals_incomplete")
    layout_bytes = bytearray()
    layout_ccsids = bytearray()
    for original, (oid, program, fmt) in zip(originals, expected, strict=True):
        _fields(
            original,
            "id program format ccsid job_before job_after receiver_base64 receiver_sha256 receiver_bytes "
            "qualified_target_base64 qualified_target_sha256 error_base64 error_sha256",
        )
        if (original["id"], original["program"], original["format"]) != (oid, program, fmt):
            raise ValueError("copy_observation_originals_mismatch")
        for key in ("job_before", "job_after"):
            _job(original[key], ccsid=True)
        if (
            original["job_before"] != original["job_after"]
            or original["ccsid"] != original["job_before"]["ccsid"]
            or type(original["ccsid"]) is not int
        ):
            raise ValueError("copy_observation_native_job_changed")
        _positive(original["receiver_bytes"], request["max_metadata_bytes"])
        receiver = _binary(
            original["receiver_base64"],
            original["receiver_sha256"],
            maximum=request["max_metadata_bytes"],
            length=original["receiver_bytes"],
        )
        if (
            len(receiver) < 8
            or int.from_bytes(receiver[:4], "big") != len(receiver)
            or int.from_bytes(receiver[4:8], "big") != len(receiver)
        ):
            raise ValueError("copy_observation_native_response_incomplete")
        if oid == "file":
            if (
                len(receiver) < 49
                or receiver[8] != file["flags_8"]
                or receiver[23] != file["flags_23"]
                or receiver[8] & 0x28
                or int.from_bytes(receiver[47:49], "big", signed=True) != 1
            ):
                raise ValueError("copy_observation_unsupported_file_cardinality_or_type")
        if oid == "rcac":
            codec = {37: "cp037", 273: "cp273", 367: "ascii"}[original["ccsid"]]
            states = (rcac["row_permissions_state"] + rcac["column_masks_state"]).encode(codec)
            if len(receiver) < 112 or receiver[21:23] != states or any(receiver[23:27]):
                raise ValueError("copy_observation_rcac_original_mismatch")
        error = _binary(original["error_base64"], original["error_sha256"], maximum=256)
        if len(error) != 16 or int.from_bytes(error[:4], "big") != 16 or any(error[4:]):
            raise ValueError("copy_observation_native_error")
        if oid in {"file", "format", "rcac"}:
            target = _binary(
                original["qualified_target_base64"], original["qualified_target_sha256"], maximum=20, length=20
            )
            layout_bytes.extend(receiver + target + error)
            layout_ccsids.extend(original["ccsid"].to_bytes(4, "big"))
        elif original["qualified_target_base64"] is not None or original["qualified_target_sha256"] is not None:
            raise ValueError("copy_observation_unexpected_target")
    if hashlib.sha256(layout_bytes + layout_ccsids).hexdigest() != layout["layout_sha256"]:
        raise ValueError("copy_observation_layout_original_mismatch")
    return deepcopy(document)


def _inventory(values, rows):
    if type(rows) is not int or rows < 0 or type(values) is not list or len(values) > 10000:
        raise ValueError("copy_observation_invalid_inventory")
    seen = set()
    for value in values:
        _fields(value, "prefix payload_key manifest_key payload_sha256 manifest_sha256 payload_bytes manifest_bytes")
        _validate_object_key(value["prefix"])
        for key in ("payload_key", "manifest_key"):
            _validate_object_key(value[key])
            if value[key] in seen:
                raise ValueError("copy_observation_duplicate_inventory")
            seen.add(value[key])
        for key in ("payload_sha256", "manifest_sha256"):
            _sha(value[key])
        for key in ("payload_bytes", "manifest_bytes"):
            _positive(value[key], 32 * 1024 * 1024)
    if bool(values) != bool(rows):
        raise ValueError("copy_observation_inventory_population")


def _validate_manifest(manifest):
    _fields(manifest, "format_version context before after snapshot_inventory rows_copied admission evidence_key")
    if manifest["format_version"] != MANIFEST_VERSION or manifest["admission"] != "not_evaluated":
        raise ValueError("copy_observation_manifest_invalid")
    _context(manifest["context"])
    _validate_object_key(manifest["evidence_key"])
    _inventory(manifest["snapshot_inventory"], manifest["rows_copied"])
    digest = context_sha256(manifest["context"])
    for phase in ("before", "after"):
        ref = ObservationReference.from_dict(manifest[phase])
        if ref.key != "copy-observations-v1/" + digest + "/" + phase + ".json":
            raise ValueError("copy_observation_reference_context_mismatch")


def _validate_pair_documents(documents):
    before, after = documents["before"], documents["after"]
    if (
        _timestamp(after["started_at"]) < _timestamp(before["completed_at"])
        or before["file"] != after["file"]
        or before["format"] != after["format"]
        or before["rcac"] != after["rcac"]
        or before["member"]["jid_hex"] != after["member"]["jid_hex"]
    ):
        raise ValueError("copy_observation_changed_during_copy")


def validate_observation_pair(*, context, before_bytes, after_bytes):
    _context(context)
    documents = {}
    for phase, data in (("before", before_bytes), ("after", after_bytes)):
        if type(data) is not bytes or not 1 <= len(data) <= context["request"]["max_metadata_bytes"]:
            raise ValueError("copy_observation_original_invalid")
        documents[phase] = validate_observation(_decode_checkpoint_json(data), context=context, phase=phase)
    _validate_pair_documents(documents)
    return documents


def publish_copy_observations(
    store, *, context, before_bytes, after_bytes, snapshot_inventory, rows_copied, evidence_key
):
    _context(context)
    _validate_object_key(evidence_key)
    _inventory(snapshot_inventory, rows_copied)
    documents = validate_observation_pair(context=context, before_bytes=before_bytes, after_bytes=after_bytes)
    refs = {}
    prefix = "copy-observations-v1/" + context_sha256(context) + "/"
    for phase, data in (("before", before_bytes), ("after", after_bytes)):
        refs[phase] = ObservationReference(prefix + phase + ".json", hashlib.sha256(data).hexdigest(), len(data))
    _validate_pair_documents(documents)
    manifest = {
        "format_version": MANIFEST_VERSION,
        "context": deepcopy(context),
        "before": refs["before"].to_dict(),
        "after": refs["after"].to_dict(),
        "snapshot_inventory": deepcopy(snapshot_inventory),
        "rows_copied": rows_copied,
        "admission": "not_evaluated",
        "evidence_key": evidence_key,
    }
    payload = encode(manifest)
    result = ObservationReference(prefix + "manifest.json", hashlib.sha256(payload).hexdigest(), len(payload))
    for phase, data in (("before", before_bytes), ("after", after_bytes)):
        store.put_once(refs[phase].key, data)
    store.put_once(result.key, payload)
    read_copy_observations(
        store,
        result,
        expected_context=context,
        expected_batches=[{k: value[k] for k in ("payload_key", "manifest_key")} for value in snapshot_inventory],
    )
    return result


def read_copy_observations(store, reference, *, expected_context=None, expected_batches=None):
    if type(reference) is not ObservationReference:
        raise ValueError("copy_observation_invalid_reference")
    manifest, _ = _read(store, reference)
    _validate_manifest(manifest)
    context = manifest["context"]
    if (
        reference.key != "copy-observations-v1/" + context_sha256(context) + "/manifest.json"
        or expected_context is not None
        and expected_context != context
    ):
        raise ValueError("copy_observation_context_mismatch")
    if (
        expected_batches is not None
        and [{k: v[k] for k in ("payload_key", "manifest_key")} for v in manifest["snapshot_inventory"]]
        != expected_batches
    ):
        raise ValueError("copy_observation_inventory_mismatch")
    documents = {}
    for phase in ("before", "after"):
        doc, _ = _read(store, ObservationReference.from_dict(manifest[phase]))
        documents[phase] = validate_observation(doc, context=context, phase=phase)
    _validate_pair_documents(documents)
    return deepcopy(manifest)


def observation_arguments(context, phase):
    _context(context)
    if phase not in {"before", "after"}:
        raise ValueError("copy_observation_invalid_phase")
    point = context["boundary"]["bound_checkpoint"]
    identity = point["receiver_identity"]
    request = context["request"]
    window, source = request["witness_window"], request["source"]
    return (
        phase,
        context_sha256(context),
        context["run_id"],
        context_sha256(context["boundary"]["capture_binding"]),
        context_sha256(context["boundary"]),
        identity["receiver_library"],
        identity["receiver_name"],
        str(point["sequence"]),
        identity["journal_library"],
        identity["journal_name"],
        window["receiver_library"],
        window["receiver_name"],
        window["start"],
        window["end"],
        source["library"],
        source["file"],
        source["member"],
        source["format"],
        str(request["max_metadata_bytes"]),
        str(request["max_seconds"]),
    )


def read_snapshot_original(item, store):
    _inventory([item], 1)
    values = {}
    for part in ("payload", "manifest"):
        try:
            data = store.get_bounded(item[part + "_key"], item[part + "_bytes"])
        except Exception:
            raise ValueError("copy_observation_snapshot_original_unavailable") from None
        if (
            type(data) is not bytes
            or len(data) != item[part + "_bytes"]
            or hashlib.sha256(data).hexdigest() != item[part + "_sha256"]
        ):
            raise ValueError("copy_observation_snapshot_original_mismatch")
        values[item[part + "_key"]] = data
    return values


def validate_inventory_originals(manifest, store_for_prefix):
    _validate_manifest(manifest)
    for item in manifest["snapshot_inventory"]:
        read_snapshot_original(item, store_for_prefix(item["prefix"]))


def validate_authority_observations(objects):
    class Objects:
        def get_bounded(self, key, maximum):
            if key not in objects or len(objects[key]) > maximum:
                raise ValueError("copy_observation_original_unavailable")
            return objects[key]

    store = Objects()
    contexts = set()
    for key, data in objects.items():
        if not key.startswith("copy-observations-v1/"):
            continue
        match = _KEY.fullmatch(key)
        if match is None:
            raise ValueError("copy_observation_invalid_authority_key")
        contexts.add(match[1])
        if len(data) > MAX_OBSERVATION_BYTES:
            raise ValueError("copy_observation_authority_budget")
    for digest in contexts:
        key = "copy-observations-v1/" + digest + "/manifest.json"
        if key not in objects:
            raise ValueError("copy_observation_completion_manifest_missing")
        data = objects[key]
        manifest = read_copy_observations(store, ObservationReference(key, hashlib.sha256(data).hexdigest(), len(data)))
        context = manifest["context"]
        from quadringent_control_plane.v2.executor.evidence import copy_intent_key

        intent = objects.get(copy_intent_key(context["pipeline_id"], context["table_id"], context["run_id"]))
        if intent is None or _decode_checkpoint_json(intent) != {
            "format_version": "quadringent-copy-intent-v1",
            "pipeline_id": context["pipeline_id"],
            "table_id": context["table_id"],
            "run_id": context["run_id"],
            "boundary": context["boundary"],
        }:
            raise ValueError("copy_observation_original_intent_mismatch")
