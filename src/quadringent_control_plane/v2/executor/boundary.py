"Preserve the pre-copy journal boundary and reject bootstrap regression. See docs/orchestration.md."

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity

BOUNDARY_FORMAT_VERSION = "quadringent-journal-boundary-v2"


class BoundaryError(ValueError):
    """Erreur de base du protocole de bascule."""


class BoundaryRegressionError(BoundaryError):
    """Une bascule enregistrée ne peut jamais être reculée.

    Levée si l'appelant tente d'enregistrer un bootstrap dont la séquence
    est strictement inférieure à un bootstrap déjà enregistré pour la même
    table, ou dont le receiver ne descend pas d'une rotation cohérente
    (retour à un receiver antérieur).
    """


@dataclass(frozen=True)
class JournalBoundary:
    """Position du journal lue en une seule requête (receiver + séquence).

    ``observed_at`` est l'horloge du control plane au moment de la lecture,
    conservée pour l'audit et la preuve — jamais utilisée pour recalculer la
    position.
    """

    receiver_library: str
    receiver_name: str
    last_sequence: int
    observed_at: datetime
    receiver_identity: ReceiverIdentity | None = None
    capture_binding: CaptureBinding | None = None
    bound_checkpoint: BoundCheckpoint | None = None

    def __post_init__(self) -> None:
        if not self.receiver_library.strip() or not self.receiver_name.strip():
            raise BoundaryError("receiver library and name required")
        if self.last_sequence < 0:
            raise BoundaryError("the handoff sequence cannot be negative")
        if self.observed_at.tzinfo is None:
            raise BoundaryError("the handoff timestamp must be timezone-aware (UTC)")
        if self.receiver_identity is not None:
            if type(self.receiver_identity) is not ReceiverIdentity or (
                self.receiver_identity.receiver_library != self.receiver_library
                or self.receiver_identity.receiver_name != self.receiver_name
            ):
                raise BoundaryError("native identity differs from the journal boundary")
        if (self.capture_binding is None) != (self.bound_checkpoint is None):
            raise BoundaryError("capture binding and bound checkpoint are both required")
        if self.capture_binding is not None:
            self.require_bound()

    def require_bound(self) -> BoundCheckpoint:
        if type(self.capture_binding) is not CaptureBinding or type(self.bound_checkpoint) is not BoundCheckpoint:
            raise BoundaryError("installed capture requires a bound journal boundary")
        if (self.bound_checkpoint.receiver_identity != self.receiver_identity
                or self.bound_checkpoint.sequence != self.last_sequence):
            raise BoundaryError("bound checkpoint differs from the original boundary")
        try:
            self.capture_binding.validate_checkpoint(self.bound_checkpoint)
        except ValueError as error:
            raise BoundaryError("journal boundary does not match its capture binding") from error
        return self.bound_checkpoint

    def with_binding(self, binding: CaptureBinding) -> "JournalBoundary":
        if self.receiver_identity is None:
            raise BoundaryError("legacy boundary cannot be adopted without native identity")
        if self.capture_binding is not None and self.capture_binding != binding:
            raise BoundaryError("original capture binding cannot be replaced")
        return replace(self, capture_binding=binding,
                       bound_checkpoint=binding.checkpoint(self.receiver_identity, self.last_sequence))

    @property
    def bootstrap_sequence(self) -> int:
        """La capture continue démarre strictement après cette position."""

        return self.last_sequence

    def to_dict(self) -> dict[str, object]:
        if self.capture_binding is not None:
            point = self.require_bound()
            return {
                "format_version": BOUNDARY_FORMAT_VERSION,
                "receiver_identity": self.receiver_identity.to_dict(),
                "capture_binding": self.capture_binding.to_dict(),
                "bound_checkpoint": point.to_dict(),
                "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            }
        if self.receiver_identity is not None:
            raise BoundaryError("native boundary must be bound before serialization")
        return {
            "receiver_library": self.receiver_library,
            "receiver_name": self.receiver_name,
            "last_sequence": self.last_sequence,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JournalBoundary":
        if type(payload) is not dict:
            raise BoundaryError("journal boundary must be an object")
        if "format_version" in payload:
            if (set(payload) != {"format_version", "receiver_identity", "capture_binding", "bound_checkpoint", "observed_at"}
                    or payload["format_version"] != BOUNDARY_FORMAT_VERSION):
                raise BoundaryError("invalid or unknown bound journal boundary format")
            try:
                identity = ReceiverIdentity.from_dict(payload["receiver_identity"])
                binding = CaptureBinding.from_dict(payload["capture_binding"])
                point = BoundCheckpoint.from_dict(payload["bound_checkpoint"])
                observed = datetime.fromisoformat(payload["observed_at"])
                return cls(identity.receiver_library, identity.receiver_name, point.sequence,
                           observed, identity, binding, point)
            except (KeyError, TypeError, ValueError) as error:
                raise BoundaryError("malformed bound journal boundary") from error
        if set(payload) != {"receiver_library", "receiver_name", "last_sequence", "observed_at"}:
            raise BoundaryError("malformed historical journal boundary")
        observed_at = payload["observed_at"]
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)
        return cls(
            receiver_library=str(payload["receiver_library"]),
            receiver_name=str(payload["receiver_name"]),
            last_sequence=int(payload["last_sequence"]),
            observed_at=observed_at,
        )


def assert_no_regression(
    previous: JournalBoundary | None, candidate: JournalBoundary
) -> None:
    """Refuse d'enregistrer un bootstrap qui reculerait la bascule.

    Sans bootstrap précédent (première copie, ou ``restart_initial_copy``
    qui repart délibérément d'une nouvelle lecture), tout candidat est
    accepté. Avec un bootstrap précédent, seules deux évolutions sont
    sûres : rester sur le même receiver avec une séquence non décroissante,
    ou passer à un receiver dont l'horodatage de bascule est postérieur (une
    rotation ne recule jamais l'horloge du journal). Un même receiver avec
    une séquence plus petite, ou un retour à un receiver antérieur, est une
    régression — signe d'une mauvaise lecture ou d'une manipulation du
    journal — et doit interrompre l'automatisme (état ``attention``).
    """

    if previous is None:
        return
    if previous.capture_binding is not None or candidate.capture_binding is not None:
        previous.require_bound()
        candidate.require_bound()
        if previous.capture_binding != candidate.capture_binding:
            raise BoundaryRegressionError("capture binding cannot change during handoff")
        if (previous.receiver_name == candidate.receiver_name
                and previous.receiver_identity != candidate.receiver_identity):
            raise BoundaryRegressionError("a receiver name cannot acquire a new native generation")
    if candidate.receiver_library == previous.receiver_library and (
        candidate.receiver_name == previous.receiver_name
    ):
        if candidate.last_sequence < previous.last_sequence:
            raise BoundaryRegressionError(
                "the handoff sequence cannot move backward on the same receiver"
            )
        return
    if candidate.observed_at < previous.observed_at:
        raise BoundaryRegressionError(
            "a receiver change cannot move the handoff timestamp backward"
        )


def plan_bootstrap(
    boundary: JournalBoundary, *, previous: JournalBoundary | None = None
) -> JournalBoundary:
    """Valide et retourne la position à enregistrer comme bootstrap explicite.

    Ne relit jamais le journal : c'est une validation pure de la position
    déjà lue (étape 1 du protocole), jamais un recalcul après la copie.
    """

    assert_no_regression(previous, boundary)
    return boundary
