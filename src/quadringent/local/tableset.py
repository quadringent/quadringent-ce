"""Environment values derived from the admission result.

Mirrors the Kubernetes manifests for one table, with the storage values of the
local backend: ``LoaderTableSpec.to_env_entry`` (loader table set),
``build_reader_deployment`` (capture) and ``build_initial_copy_job`` (copy).
Parity tests feed these values to the real configuration parsers.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Mapping

from ..storage_layout import journal_prefix

RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class AdmissionError(ValueError):
    """The admission result of the container is incomplete or inconsistent."""


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def evidence_key(raw_prefix: str, table_id: str, run_id: str) -> str:
    """Same formula as ``v2/executor/evidence.py::evidence_key`` (parity test)."""
    return f"{raw_prefix.rstrip('/')}/{table_id}/evidence/{run_id}.json"


def _bootstrap(boundary: Mapping[str, Any]) -> dict[str, str]:
    try:
        identity = boundary["receiver_identity"]
        sequence = boundary["bound_checkpoint"]["sequence"]
        observed_at = datetime.fromisoformat(boundary["observed_at"]).isoformat()
        values = {
            "AS400_BOOTSTRAP_RECEIVER": identity["receiver_name"],
            "AS400_BOOTSTRAP_RECEIVER_LIBRARY": identity["receiver_library"],
            "AS400_BOOTSTRAP_SEQUENCE": str(int(sequence)),
            "AS400_BOOTSTRAP_OBSERVED_AT": observed_at,
        }
    except (KeyError, TypeError, ValueError):
        raise AdmissionError("the bound boundary is incomplete") from None
    if not all(values.values()):
        raise AdmissionError("the bound boundary is incomplete")
    return values


def parse_admission(document: Mapping[str, Any], *, table_id: str, pipeline_id: str) -> dict[str, Any]:
    """Validate the ``admit`` result and return it unchanged."""
    keys = {"binding", "boundary", "run_id", "evidence_key", "raw_prefix_root", "stream_key", "table_id", "pipeline_id"}
    if not isinstance(document, Mapping) or not keys <= set(document):
        raise AdmissionError("the admission result is incomplete")
    if not RUN_ID.fullmatch(str(document["run_id"])):
        raise AdmissionError("the admission run id is not a UUID")
    if (document["table_id"], document["pipeline_id"]) != (table_id, pipeline_id):
        raise AdmissionError("the admission belongs to another table or pipeline")
    if document["evidence_key"] != evidence_key(str(document["raw_prefix_root"]), table_id, str(document["run_id"])):
        raise AdmissionError("the evidence key does not follow the expected layout")
    _bootstrap(document["boundary"])
    return dict(document)


def loader_table_entry(
    admission: Mapping[str, Any], *, schema: str, table: str, key_columns: tuple[str, ...],
    columns: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    """One entry of ``QUADRINGENT_LOADER_TABLE_SET_JSON`` for a bound table."""
    return {
        "table_id": admission["table_id"],
        "schema": schema,
        "table": table,
        "key_columns": list(key_columns),
        "columns": [dict(column) for column in columns],
        "evidence_key": admission["evidence_key"],
        "pipeline_id": admission["pipeline_id"],
        "run_id": admission["run_id"],
        "capture_binding": admission["binding"],
    }


def capture_identity_env(admission: Mapping[str, Any], *, table: str, journal_library: str, journal_name: str) -> dict[str, str]:
    """Identity and position values of the continuous capture (single table mode)."""
    bootstrap = _bootstrap(admission["boundary"])
    raw_prefix = journal_prefix(str(admission["raw_prefix_root"]), table)
    return {
        "AS400_STREAM_KEY": str(admission["stream_key"]),
        "AS400_CHECKPOINT_SCOPE": "source-raw-v1",
        "AS400_JOURNAL_LIBRARY": journal_library.strip().upper(),
        "AS400_JOURNAL_NAME": journal_name.strip().upper(),
        "AS400_RAW_PREFIX": raw_prefix,
        "AS400_RECEIPTED_SCANS": "true",
        "AS400_BOOTSTRAP_RECEIVER": bootstrap["AS400_BOOTSTRAP_RECEIVER"],
        "AS400_BOOTSTRAP_RECEIVER_LIBRARY": bootstrap["AS400_BOOTSTRAP_RECEIVER_LIBRARY"],
        "AS400_BOOTSTRAP_SEQUENCE": bootstrap["AS400_BOOTSTRAP_SEQUENCE"],
        "AS400_CAPTURE_BINDING_JSON": dumps(admission["binding"]),
        "AS400_BOUNDARY_JSON": dumps(admission["boundary"]),
    }
