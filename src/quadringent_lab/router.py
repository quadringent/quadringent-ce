"""Lab-only router: choose the scenario and inject boundary conditions.

Built and mounted by ``LabGateway`` exclusively. The product application
(``create_v2_app``) never imports this module.
"""

from __future__ import annotations

import os
import threading

from fastapi import FastAPI, HTTPException, Request

from . import LAB_MARKER_FILE
from .conditions import ConditionError
from .faults import FaultError, FaultRule
from .scenarios import SCENARIOS, UnknownScenarioError


def build_admin_app(runtime) -> FastAPI:  # noqa: ANN001 - LabRuntime, avoids an import cycle
    app = FastAPI(title="Quadringent laboratory control", openapi_url=None, docs_url=None, redoc_url=None)

    async def body(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        import json

        try:
            value = json.loads(raw)
        except ValueError as error:
            raise HTTPException(400, "invalid JSON") from error
        if not isinstance(value, dict):
            raise HTTPException(400, "JSON object required")
        return value

    @app.get("/lab/identity")
    async def identity() -> dict:
        return {
            "lab": True,
            "pid": os.getpid(),
            "port": runtime.port,
            "workdir": str(runtime.workdir),
            "marker": LAB_MARKER_FILE,
            "scenario": runtime.state.scenario,
        }

    @app.get("/lab/state")
    async def state() -> dict:
        return runtime.public_state()

    @app.get("/lab/scenarios")
    async def scenarios() -> dict:
        return {"items": [scenario.to_dict() for scenario in SCENARIOS], "current": runtime.state.scenario}

    @app.put("/lab/scenario")
    async def activate(request: Request) -> dict:
        payload = await body(request)
        name = payload.get("name")
        if not isinstance(name, (str, int)):
            raise HTTPException(400, "name is required")
        try:
            scenario = runtime.activate(name)
        except UnknownScenarioError as error:
            raise HTTPException(404, f"unknown scenario: {name}") from error
        return {"scenario": scenario.to_dict(), "state": runtime.public_state()}

    @app.post("/lab/reset")
    async def reset() -> dict:
        runtime.reset()
        return runtime.public_state()

    @app.put("/lab/conditions")
    async def conditions(request: Request) -> dict:
        payload = await body(request)
        try:
            runtime.patch_conditions(payload)
        except ConditionError as error:
            raise HTTPException(400, str(error)) from error
        return runtime.public_state()

    @app.get("/lab/faults")
    async def list_faults() -> dict:
        return {"items": [rule.to_dict() for rule in runtime.faults.rules()]}

    @app.put("/lab/faults")
    async def add_fault(request: Request) -> dict:
        payload = await body(request)
        try:
            runtime.faults.add(FaultRule(**payload))
        except (TypeError, FaultError) as error:
            raise HTTPException(400, str(error)) from error
        return {"items": [rule.to_dict() for rule in runtime.faults.rules()]}

    @app.delete("/lab/faults")
    async def clear_faults() -> dict:
        runtime.faults.clear()
        return {"items": []}

    @app.get("/lab/ledger")
    async def ledger(boundary: str | None = None) -> dict:
        return {"items": runtime.state.ledger(boundary)}

    @app.post("/lab/shutdown")
    async def shutdown() -> dict:
        if runtime.shutdown_callback is None:
            raise HTTPException(403, "shutdown over HTTP is disabled (start with --allow-shutdown)")
        threading.Timer(0.2, runtime.shutdown_callback).start()
        return {"stopping": True}

    return app
