"""Shared helpers for the laboratory tests (no tests of its own).

The laboratory runs the real v2 app over a throwaway SQLite file; nothing
here opens a connection outside the loopback interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from fastapi.testclient import TestClient

from quadringent_lab import fixtures
from quadringent_lab.gateway import LabGateway
from quadringent_lab.runtime import LabRuntime

BASE_URL = "http://127.0.0.1:8877"
REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_ROUTE_FILE = REPO_ROOT / "src" / "quadringent_control_plane" / "v2" / "routes" / "diagnostics.py"


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


class Session:
    """A TestClient with its own cookie jar and an optional API prefix."""

    def __init__(self, gateway: LabGateway, prefix: str = "") -> None:
        self.client = TestClient(gateway, base_url=BASE_URL)
        self.prefix = prefix

    def login(self, email: str, password: str = fixtures.LAB_PASSWORD) -> "Session":
        response = self.client.post(f"{self.prefix}/v2/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return self

    def get(self, path: str, **kwargs):
        return self.client.get(f"{self.prefix}{path}", **kwargs)

    def post(self, path: str, json=None, *, headers: dict[str, str] | None = None):
        return self.client.post(f"{self.prefix}{path}", json=json if json is not None else {}, headers=headers or key())

    def patch(self, path: str, json=None, *, headers: dict[str, str] | None = None):
        return self.client.patch(f"{self.prefix}{path}", json=json if json is not None else {}, headers=headers or key())


@dataclass
class Lab:
    runtime: LabRuntime
    gateway: LabGateway

    def session(self, who: str = "operator") -> Session:
        email, prefix = {
            "operator": (fixtures.OPERATOR_EMAIL, ""),
            "reader": (fixtures.READER_EMAIL, ""),
            "other": (fixtures.OTHER_ADMIN_EMAIL, "/lab-other"),
        }[who]
        return Session(self.gateway, prefix).login(email)

    def anonymous(self) -> Session:
        return Session(self.gateway)

    def admin(self) -> TestClient:
        return TestClient(self.gateway, base_url=BASE_URL)

    def activate(self, scenario: str) -> None:
        self.runtime.activate(scenario)

    def executor_events(self, pipeline_id: str | None = None) -> list[str]:
        return [
            str(entry["event"])
            for entry in self.runtime.state.ledger("pipeline_executor")
            if pipeline_id is None or entry["pipeline_id"] == pipeline_id
        ]

    def diagnostics_available(self) -> bool:
        return "/v2/diagnostics/report" in self.runtime.apps["lab"].openapi().get("paths", {})


def build_lab(workdir: Path, scenario: str) -> Lab:
    runtime = LabRuntime(workdir, scenario=scenario, ui_dist=None)
    return Lab(runtime=runtime, gateway=LabGateway(runtime))


DIAGNOSTICS_SKIP_REASON = (
    "the /v2/diagnostics/report route is not wired into create_v2_app yet "
    "(coordinator-owned routes/diagnostics.py); the assertion activates automatically once it is"
)
