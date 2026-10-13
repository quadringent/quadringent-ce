"""Administrator-only diagnostic download; no live provider calls."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..auth import require_scope
from ..errors import ApiError, wrong_environment, store_unavailable
from ..services.diagnostics import DiagnosticsService, FORMAT_VERSION

router = APIRouter(prefix="/v2/diagnostics", tags=["diagnostics"])
_admin = require_scope("admin")


def _identity(request: Request):
    identity = _admin(request)
    if identity.subject == "anonymous":
        raise ApiError(401, "invalid_request", "Diagnostic export requires authentication", next_action="sign in as an administrator", retryable=False)
    if identity.source_restriction:
        raise wrong_environment("Organization diagnostic export is outside this token's source restriction")
    return identity


@router.get("")
async def capabilities(request: Request, identity=Depends(_identity)) -> JSONResponse:
    return JSONResponse({"export": {"state": "available", "format_version": FORMAT_VERSION, "collection": "local_database_snapshot"}}, headers={"Cache-Control": "no-store"})


@router.get("/export")
async def download(request: Request, identity=Depends(_identity)) -> JSONResponse:
    try:
        snapshot = DiagnosticsService(request.app.state.engine, org_id=request.app.state.org_id).snapshot()
    except SQLAlchemyError as error:
        raise store_unavailable("Diagnostic snapshot unavailable; check the control plane database") from error
    snapshot["api_schema_version"] = request.app.version
    return JSONResponse(snapshot, headers={
        "Content-Disposition": 'attachment; filename="quadringent-diagnostic.json"',
        "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
    })
