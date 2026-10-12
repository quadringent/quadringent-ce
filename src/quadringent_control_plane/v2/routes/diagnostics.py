"""``GET /v2/diagnostics`` and ``GET /v2/diagnostics/export``.

Same access control as the other read routes (``require_scope("read")``) and
the same organization scoping (``app.state.org_id``). Reading a diagnostic
never repairs anything. The shared ``quadringent.diagnostics`` modules are
imported on use: an image that does not ship them still starts and answers
these routes with an explicit 503.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_scope
from ...version import version
from ..errors import ApiError, not_found

router = APIRouter(prefix="/v2/diagnostics", tags=["diagnostics"])


def _unavailable() -> ApiError:
    return ApiError(503, "capability_unavailable", "Diagnostics are not packaged in this control plane image.",
                    next_action="use quadringent doctor locally, or upgrade the control plane image",
                    retryable=False)


def _service(request: Request):
    try:
        from ..services.diagnostics import DiagnosticsService
    except ImportError as error:
        raise _unavailable() from error
    provider = getattr(request.app.state, "pipeline_observation_provider", None)
    reader = getattr(provider, "evidence", None)
    return DiagnosticsService(request.app.state.engine, org_id=request.app.state.org_id, version=version(),
                              evidence_reader=reader if callable(reader) else None)


@router.get("")
async def get_diagnostics(
    request: Request, pipeline_id: str | None = None, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    from ..services.diagnostics import DiagnosticsPipelineNotFoundError
    try:
        return service.report(pipeline_id=pipeline_id).to_dict()
    except DiagnosticsPipelineNotFoundError as error:
        raise not_found("Pipeline not found") from error


@router.get("/export")
async def get_diagnostics_export(request: Request, identity=Depends(require_scope("read"))) -> dict[str, object]:
    service = _service(request)
    try:
        from quadringent.diagnostics.export import build_export
    except ImportError as error:
        raise _unavailable() from error
    return build_export(service.report(), extra_identifiers=service.sensitive_identifiers()).to_document()
