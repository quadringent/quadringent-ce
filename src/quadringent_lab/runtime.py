"""Laboratory runtime: engine, boundary fakes, the two organization apps."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from quadringent_control_plane.v2 import db as v2_db
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.destinations import DestinationsService
from quadringent_control_plane.v2.services.sources import SourcesService

from . import LAB_OTHER_ORG_ID, LAB_ORG_ID, fixtures
from .boundaries import (
    LabCostsProvider,
    LabDestinationVerifier,
    LabLogSource,
    LabObservationProvider,
    LabPipelineExecutor,
    LabSourceProbe,
    LabTableDiscovery,
)
from .conditions import BoundaryConditions
from .faults import FaultRegistry, FaultRule
from .scenarios import DEFAULT_SCENARIO, Scenario, get_scenario
from .seed import OtherOrgDiscovery, SeedResult, seed
from .state import LabState


class LabRuntime:
    """Owns one throwaway SQLite database and the replaced boundaries.

    ``reseed=False`` attaches to an existing laboratory database (used to
    simulate a service restart over the same file).
    """

    def __init__(
        self,
        workdir: Path,
        *,
        scenario: str = DEFAULT_SCENARIO,
        reseed: bool = True,
        ui_dist: Path | None = None,
        shutdown: Callable[[], None] | None = None,
        port: int | None = None,
    ) -> None:
        self.workdir = Path(workdir)
        self.ui_dist = ui_dist
        self.port = port
        self.shutdown_callback = shutdown
        self.state = LabState()
        self.faults = FaultRegistry()
        self._lock = threading.RLock()

        dsn = f"sqlite:///{self.workdir / 'lab.sqlite3'}"
        self.dsn = dsn
        v2_db.run_migrations(dsn)
        self.engine: Engine = v2_db.create_engine_for(dsn)
        self.secret_box = SecretBox(fixtures.SECRET_KEY)

        self.probe = LabSourceProbe(self.state)
        self.discovery = LabTableDiscovery(self.state)
        self.verifier = LabDestinationVerifier(self.state)
        self.executor = LabPipelineExecutor(self.state)
        self.observation = LabObservationProvider(self.state)
        self.logs = LabLogSource(self.state)
        self.costs = LabCostsProvider()

        self.apps: dict[str, FastAPI] = {
            LAB_ORG_ID: self._build_app(LAB_ORG_ID, self.discovery),
            LAB_OTHER_ORG_ID: self._build_app(LAB_OTHER_ORG_ID, OtherOrgDiscovery()),
        }
        self.seed_result = SeedResult()
        if reseed:
            self.activate(scenario)
        else:
            self.state.scenario = get_scenario(scenario).name

    def _build_app(self, org_id: str, discovery: object) -> FastAPI:
        return create_v2_app(
            engine=self.engine,
            secret_box=self.secret_box,
            org_id=org_id,
            token_pepper=fixtures.TOKEN_PEPPER,
            require_authentication=True,
            source_probe=self.probe,
            table_discovery_client=discovery,  # type: ignore[arg-type]
            destination_verifier=self.verifier,
            pipeline_executor=self.executor,
            pipeline_observation_provider=self.observation,
            log_source=self.logs,
            costs_provider=self.costs,
            # A slow poll keeps the browser's event stream cheap on SQLite.
            sse_poll_interval=1.0,
            enable_mcp=False,
        )

    # -- scenario control -----------------------------------------------------
    def activate(self, identifier: str | int) -> Scenario:
        """Reset the database, inject the scenario's conditions and faults."""

        scenario = get_scenario(identifier)
        with self._lock:
            self.state.conditions = BoundaryConditions().patched(dict(scenario.conditions))
            self.state.scenario = scenario.name
            self.state.generation += 1
            self.state.clear_ledger()
            self.seed_result = seed(
                self.engine, self.secret_box, self.discovery, canaries=self.state.conditions.canaries
            )
            self._prepare(scenario)
            self.state.clear_ledger()
            self.faults.replace([FaultRule(**fault) for fault in scenario.faults])  # type: ignore[arg-type]
        return scenario

    def reset(self) -> Scenario:
        return self.activate(self.state.scenario)

    def patch_conditions(self, changes: dict[str, object]) -> BoundaryConditions:
        with self._lock:
            self.state.conditions = self.state.conditions.patched(changes)
            return self.state.conditions

    def _prepare(self, scenario: Scenario) -> None:
        """Run prepare steps through the product services with an ``ok`` boundary."""

        if not scenario.prepare:
            return
        neutral = LabState(conditions=BoundaryConditions())
        if "source_authenticated" in scenario.prepare:
            SourcesService(self.engine, self.secret_box, org_id=LAB_ORG_ID).test(
                fixtures.SOURCE_ID, probe=LabSourceProbe(neutral)
            )
        if "destination_verified" in scenario.prepare:
            DestinationsService(self.engine, self.secret_box, org_id=LAB_ORG_ID).verify(
                fixtures.DESTINATION_ID, verifier=LabDestinationVerifier(neutral)
            )

    def public_state(self) -> dict[str, object]:
        return {
            "lab": True,
            "scenario": self.state.scenario,
            "generation": self.state.generation,
            "conditions": self.state.conditions.to_dict(),
            "faults": [rule.to_dict() for rule in self.faults.rules()],
            "seed": self.seed_result.public(),
            "credentials": {
                "operator": {"email": fixtures.OPERATOR_EMAIL, "password": fixtures.LAB_PASSWORD, "role": "admin"},
                "reader": {"email": fixtures.READER_EMAIL, "password": fixtures.LAB_PASSWORD, "role": "reader"},
                "other_admin": {
                    "email": fixtures.OTHER_ADMIN_EMAIL,
                    "password": fixtures.LAB_PASSWORD,
                    "role": "admin",
                    "api_prefix": "/lab-other",
                },
            },
            "note": "All data is synthetic (provenance lab_fixture).",
        }

    def close(self) -> None:
        self.engine.dispose()
