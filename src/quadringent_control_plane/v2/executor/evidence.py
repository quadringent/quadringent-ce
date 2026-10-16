"""Preuve durable de fin de copie initiale (autorise ``copying -> live``).

La preuve est un petit objet JSON écrit par le Job de copie initiale
lui-même (jamais par le control plane, qui ne peut pas savoir si la copie a
réellement réussi) sur le stockage objet déjà en place
(`quadringent.storage_backend.StorageBackend.object_store`). Le control
plane ne fait que la *lire* pour décider la transition ; il ne l'écrit
jamais, à l'image du principe déjà en place pour les checkpoints de
lecteur (source de vérité = ce que le Job a durablement publié).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol

from .boundary import JournalBoundary
from quadringent.checkpoint import _decode_checkpoint_json
from quadringent.receiver_identity import BoundCheckpoint

from quadringent.copy_observation import ObservationReference, read_copy_observations

MAX_EVIDENCE_BYTES = 65_536
EVIDENCE_FORMAT_VERSION = "quadringent-initial-copy-evidence-v2"
OBSERVED_EVIDENCE_FORMAT_VERSION = "quadringent-initial-copy-evidence-v3"


class EvidenceError(ValueError):
    """Preuve absente ou mal formée."""


@dataclass(frozen=True)
class SnapshotBatchRef:
    """Référence d'un lot d'instantané déjà publié (voir ``as400_snapshot_
    publish.publish_snapshot_batches``)."""

    payload_key: str
    manifest_key: str

    def to_dict(self) -> dict[str, str]:
        return {"payload_key": self.payload_key, "manifest_key": self.manifest_key}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SnapshotBatchRef":
        if (type(payload) is not dict or set(payload) != {"payload_key", "manifest_key"}
                or any(type(value) is not str or not value for value in payload.values())):
            raise ValueError("invalid snapshot batch reference")
        return cls(payload_key=payload["payload_key"], manifest_key=payload["manifest_key"])


@dataclass(frozen=True)
class InitialCopyEvidence:
    pipeline_id: str
    table_id: str
    run_id: str
    boundary: JournalBoundary
    rows_copied: int
    completed_at: datetime
    # Clés déjà publiées de l'instantané — sans capacité de listage générique
    # sur ``ObjectStore`` (choix délibéré du dépôt), c'est le seul moyen pour
    # le chargeur de les retrouver. Défaut vide pour une preuve écrite avant
    # ce champ (aucune en production au 24 septembre 2026 : le chargeur n'a
    # jamais rien chargé).
    snapshot_batches: tuple[SnapshotBatchRef, ...] = ()
    copy_observations: ObservationReference | None = None

    def require_bound(self) -> BoundCheckpoint:
        return self.boundary.require_bound()

    def to_dict(self) -> dict[str, object]:
        document = {
            "pipeline_id": self.pipeline_id,
            "table_id": self.table_id,
            "run_id": self.run_id,
            "boundary": self.boundary.to_dict(),
            "rows_copied": self.rows_copied,
            "completed_at": self.completed_at.astimezone(timezone.utc).isoformat(),
            "snapshot_batches": [batch.to_dict() for batch in self.snapshot_batches],
        }
        if self.boundary.capture_binding is not None:
            self.require_bound()
            document["format_version"] = EVIDENCE_FORMAT_VERSION
            if self.copy_observations is not None:
                document["format_version"] = OBSERVED_EVIDENCE_FORMAT_VERSION
                document["copy_observations"] = self.copy_observations.to_dict()
        elif self.copy_observations is not None:
            raise EvidenceError("observations require a bound copy")
        return document

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "InitialCopyEvidence":
        try:
            if "format_version" in payload:
                fields = {"format_version", "pipeline_id", "table_id", "run_id", "boundary",
                          "rows_copied", "completed_at", "snapshot_batches"}
                version = payload["format_version"]
                if version == OBSERVED_EVIDENCE_FORMAT_VERSION:
                    fields.add("copy_observations")
                if version not in {EVIDENCE_FORMAT_VERSION, OBSERVED_EVIDENCE_FORMAT_VERSION} or set(payload) != fields:
                    raise ValueError("unknown bound initial-copy evidence format")
                for name in ("pipeline_id", "table_id", "run_id"):
                    if type(payload[name]) is not str or not payload[name] or payload[name].strip() != payload[name]:
                        raise ValueError("invalid evidence identity")
                if type(payload["rows_copied"]) is not int or payload["rows_copied"] < 0:
                    raise ValueError("invalid copied row count")
            completed_at = payload["completed_at"]
            if isinstance(completed_at, str):
                completed_at = datetime.fromisoformat(completed_at)
            raw_batches = payload.get("snapshot_batches") or []
            if not isinstance(raw_batches, list):
                raise ValueError("snapshot_batches must be a list")
            evidence = cls(
                pipeline_id=str(payload["pipeline_id"]),
                table_id=str(payload["table_id"]),
                run_id=str(payload["run_id"]),
                boundary=JournalBoundary.from_dict(payload["boundary"]),
                rows_copied=int(payload["rows_copied"]),
                completed_at=completed_at,
                snapshot_batches=tuple(SnapshotBatchRef.from_dict(item) for item in raw_batches),
                copy_observations=(ObservationReference.from_dict(payload["copy_observations"])
                                   if payload.get("format_version") == OBSERVED_EVIDENCE_FORMAT_VERSION else None),
            )
            if "format_version" in payload:
                if evidence.completed_at.tzinfo is None:
                    raise ValueError("evidence timestamp must be timezone-aware")
                evidence.require_bound()
            elif evidence.boundary.capture_binding is not None:
                raise ValueError("bound initial-copy evidence requires its version")
            return evidence
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceError("malformed initial-copy evidence") from error


def evidence_key(raw_prefix: str, table_id: str, run_id: str) -> str:
    return f"{raw_prefix.rstrip('/')}/{table_id}/evidence/{run_id}.json"


def copy_intent_key(pipeline_id: str, table_id: str, run_id: str) -> str:
    """Return the immutable copy-intent key within the registry authority."""
    for value in (pipeline_id, table_id, run_id):
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError("invalid copy-intent identity")
    identity = json.dumps([pipeline_id, table_id, run_id], separators=(",", ":")).encode()
    return "copy-intents-v1/" + hashlib.sha256(identity).hexdigest() + ".json"


class ObjectStoreProtocol(Protocol):
    """Sous-ensemble de `quadringent.object_store.ObjectStore` utilisé ici."""

    def get_bounded(self, key: str, max_bytes: int) -> bytes: ...


class EvidenceReader:
    """Lit la preuve de copie initiale, jamais ne l'écrit."""

    def __init__(self, object_store: ObjectStoreProtocol) -> None:
        self._object_store = object_store

    def read(self, key: str, *, expected_source: tuple[str, str] | None = None) -> InitialCopyEvidence | None:
        try:
            raw = self._object_store.get_bounded(key, MAX_EVIDENCE_BYTES)
        except FileNotFoundError:
            return None
        try:
            payload = _decode_checkpoint_json(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise EvidenceError("initial-copy evidence unreadable") from error
        if not isinstance(payload, dict):
            raise EvidenceError("malformed initial-copy evidence")
        evidence = InitialCopyEvidence.from_dict(payload)
        if evidence.copy_observations is not None:
            try:
                class Authority:
                    def get_bounded(_, key, maximum):
                        return self._object_store.get_bounded("receiver-authority-v1/" + key, maximum)

                manifest = read_copy_observations(Authority(), evidence.copy_observations,
                                                 expected_batches=[ref.to_dict() for ref in evidence.snapshot_batches])
                context = manifest["context"]
                if manifest['evidence_key'] != key:
                    raise ValueError('observation evidence key differs')
                if expected_source is not None and (context['request']['source']['library'], context['request']['source']['file']) != expected_source:
                    raise ValueError('observation source differs')
                if ((context["pipeline_id"], context["table_id"], context["run_id"], context["boundary"], manifest["rows_copied"])
                        != (evidence.pipeline_id, evidence.table_id, evidence.run_id, evidence.boundary.to_dict(), evidence.rows_copied)):
                    raise ValueError("copy observation context differs")
            except (ValueError, FileNotFoundError, TypeError) as error:
                raise EvidenceError("copy observation originals unavailable or different") from error
        return evidence
