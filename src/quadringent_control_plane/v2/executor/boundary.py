"Preserve the pre-copy journal boundary and reject bootstrap regression. See docs/orchestration.md."

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


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

    def __post_init__(self) -> None:
        if not self.receiver_library.strip() or not self.receiver_name.strip():
            raise BoundaryError("receiver library and name required")
        if self.last_sequence < 0:
            raise BoundaryError("the handoff sequence cannot be negative")
        if self.observed_at.tzinfo is None:
            raise BoundaryError("the handoff timestamp must be timezone-aware (UTC)")

    @property
    def bootstrap_sequence(self) -> int:
        """La capture continue démarre strictement après cette position."""

        return self.last_sequence

    def to_dict(self) -> dict[str, object]:
        return {
            "receiver_library": self.receiver_library,
            "receiver_name": self.receiver_name,
            "last_sequence": self.last_sequence,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JournalBoundary":
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
