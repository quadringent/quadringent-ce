"""Mutable laboratory state shared by the boundary fakes and the lab router."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading

from .conditions import BoundaryConditions


@dataclass
class LabState:
    """Current scenario, injected conditions and the call ledger.

    The ledger records what crossed each replaced boundary (never a secret),
    so a test can prove an effect happened exactly once or not at all.
    """

    conditions: BoundaryConditions = field(default_factory=BoundaryConditions)
    scenario: str = "ready"
    generation: int = 0
    _ledger: list[dict[str, object]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, boundary: str, **details: object) -> None:
        entry = {"boundary": boundary, "at": datetime.now(timezone.utc).isoformat(), **details}
        with self._lock:
            self._ledger.append(entry)

    def ledger(self, boundary: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            entries = list(self._ledger)
        if boundary is None:
            return entries
        return [entry for entry in entries if entry["boundary"] == boundary]

    def clear_ledger(self) -> None:
        with self._lock:
            self._ledger.clear()
