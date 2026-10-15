"""Injected boundary conditions.

A scenario is a named bundle of these conditions plus a seed. Each field
selects the behavior of one boundary fake. Nothing here reaches a network.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace

PROBE_MODES = ("ok", "network_down", "tls_invalid", "auth_denied")
DISCOVERY_MODES = ("ready", "journal_absent", "journal_config", "no_key")
VERIFIER_MODES = ("ok", "insufficient_privileges", "connection_failed")
EVIDENCE_MODES = ("simulation", "live", "historical", "absent", "stale", "reading_only")
EXECUTOR_MODES = ("ok", "unavailable")


class ConditionError(ValueError):
    """An unknown condition name or value was requested."""


@dataclass(frozen=True)
class BoundaryConditions:
    """Behavior of every replaced boundary.

    ``evidence`` is the default mode for every pipeline of the main
    organization; ``evidence_overrides`` refines it per pipeline id. ``live``
    is a laboratory-injected condition (never a measurement): it exists only
    to exercise the "green" path of the diagnostics.
    """

    probe: str = "ok"
    discovery: str = "ready"
    verifier: str = "ok"
    evidence: str = "simulation"
    executor: str = "ok"
    canaries: bool = False
    evidence_overrides: dict[str, str] = field(default_factory=dict)

    def evidence_for(self, pipeline_id: str) -> str:
        return self.evidence_overrides.get(pipeline_id, self.evidence)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def patched(self, changes: dict[str, object]) -> "BoundaryConditions":
        allowed = {item.name for item in fields(self)}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ConditionError(f"unknown condition(s): {', '.join(unknown)}")
        result = replace(self, **changes)  # type: ignore[arg-type]
        result.validate()
        return result

    def validate(self) -> None:
        for name, value, allowed in (
            ("probe", self.probe, PROBE_MODES),
            ("discovery", self.discovery, DISCOVERY_MODES),
            ("verifier", self.verifier, VERIFIER_MODES),
            ("evidence", self.evidence, EVIDENCE_MODES),
            ("executor", self.executor, EXECUTOR_MODES),
        ):
            if value not in allowed:
                raise ConditionError(f"{name} must be one of {allowed}, got {value!r}")
        for pipeline_id, mode in self.evidence_overrides.items():
            if mode not in EVIDENCE_MODES:
                raise ConditionError(f"evidence override for {pipeline_id!r} must be one of {EVIDENCE_MODES}")
        if not isinstance(self.canaries, bool):
            raise ConditionError("canaries must be a boolean")
