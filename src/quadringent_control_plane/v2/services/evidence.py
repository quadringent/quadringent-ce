"""Bounded public evidence; process state never substitutes for a measurement."""
from dataclasses import asdict, dataclass, field
from typing import Literal

EvidenceState = Literal["observed", "stale", "absent"]


@dataclass(frozen=True)
class EvidenceStage:
    state: EvidenceState = "absent"
    observed_at: str | None = None
    event_count: int | None = None
    lag_seconds: float | None = None
    duplicate_count: int | None = None
    detail: str = "Not measured."


@dataclass(frozen=True)
class PipelineEvidence:
    pipeline_id: str
    provenance: str = "absent"
    evidence_kind: str = "absent"
    collected_at: str | None = None
    reading: EvidenceStage = field(default_factory=EvidenceStage)
    delivery: EvidenceStage = field(default_factory=EvidenceStage)
    reconciliation: EvidenceStage = field(default_factory=EvidenceStage)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def absent_evidence(pipeline_id: str, *, reason: str) -> PipelineEvidence:
    stage = EvidenceStage(detail=reason)
    return PipelineEvidence(pipeline_id, reading=stage, delivery=stage, reconciliation=stage)
