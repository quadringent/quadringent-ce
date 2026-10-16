"""Routes ``/v2/pipelines/{id}/actions/*`` (tâches 5, 6, 7).

``pause``/``resume`` s'exécutent directement. ``remove``,
``restart_initial_copy`` et ``replay`` sont des actions sensibles (tâche 7,
contrat §2.4) : sans ``confirmation_token`` valide dans le corps, elles
créent une confirmation ``pending`` et renvoient
``409 pending_confirmation_required`` plutôt que de s'exécuter. L'exécuteur
réel (Kubernetes) n'est jamais câblé : ``request.app.state.pipeline_executor``
est ``None`` par défaut (échec fermé, 503) et les tests injectent un faux
exécuteur.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import (
    ApiError,
    capability_unavailable,
    invalid_request,
    not_found,
    pending_confirmation_required,
    wrong_confirmation,
)
from ..http import AuditContext, envelope, idempotent_write
from ..services.confirmations import ConfirmationNotFoundError, ConfirmationsService
from ..services.logs import KNOWN_LEVELS, LogsService, NullLogSource
from ..services.evidence import absent_evidence
from ..services.logs import redact as redact_log_message
from ..services.native_context import context_snapshot
from ..services.native_verifications import NativeVerificationsService
from ..services.observation import NullObservationProvider, PipelineObservationProviderProtocol
from ..services.pipelines import (
    ACTIONS,
    CONFIRMATION_REQUIRED_ACTIONS,
    InvalidListFilterError,
    PipelineExecutorUnavailableError,
    PipelineNotFoundError,
    PipelinesService,
)
from ..services.state_machine import ForbiddenTransitionError, UnknownStateError

router = APIRouter(prefix="/v2/pipelines", tags=["pipelines"])


def _service(request: Request) -> PipelinesService:
    return PipelinesService(request.app.state.engine, org_id=request.app.state.org_id)


def _confirmations_service(request: Request) -> ConfirmationsService:
    return ConfirmationsService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


def _observation_provider(request: Request) -> PipelineObservationProviderProtocol:
    provider = getattr(request.app.state, "pipeline_observation_provider", None)
    return provider if provider is not None else NullObservationProvider()


def _native_service(request: Request, identity, *, preparer=None, controller=None) -> NativeVerificationsService:
    return NativeVerificationsService(
        request.app.state.engine, org_id=request.app.state.org_id,
        source_restriction=identity.source_restriction, preparer=preparer, controller=controller,
    )


def _dispatch_native_verification(service, pipeline_id, *, requested_by, idempotency_key):
    lease = None
    try:
        attempt = service.request(pipeline_id, requested_by=requested_by, idempotency_key=idempotency_key)
        if attempt["lifecycle"] == "finished":
            return attempt
        lease = service.claim(attempt["id"], holder="http-" + uuid.uuid4().hex, lease_seconds=7200)
        prepared_lease = service.prepare(lease)
        if prepared_lease is None:
            return service.get(attempt["id"])
        lease = prepared_lease
        return service.finalize(lease)
    except Exception:
        if lease is not None:
            try:
                return service.close_incomplete(lease, reason_code="controller_failed")
            except Exception:
                # A lost lease belongs to its new holder; never override that fence.
                pass
        raise capability_unavailable("Native verification could not complete in the current context.") from None


@router.get("/{pipeline_id}/native-verifications")
async def list_native_verifications(
    pipeline_id: str, request: Request, limit: int = 50, identity=Depends(require_scope("read")),
) -> dict[str, object]:
    try:
        items = _native_service(request, identity).list_history(pipeline_id, limit=limit)
    except LookupError:
        raise not_found("Pipeline not found") from None
    except ValueError:
        raise invalid_request("Native history limit must be between 1 and 500.") from None
    return {"items": list(items)}


@router.get("/{pipeline_id}/native-verifications/latest")
async def get_latest_native_verification(
    pipeline_id: str, request: Request, identity=Depends(require_scope("read")),
) -> dict[str, object] | None:
    try:
        return _native_service(request, identity).get_latest(pipeline_id)
    except LookupError:
        raise not_found("Pipeline not found") from None


@router.get("/{pipeline_id}/native-verifications/{verification_id}")
async def get_native_verification(
    pipeline_id: str, verification_id: str, request: Request, identity=Depends(require_scope("read")),
) -> dict[str, object]:
    try:
        result = _native_service(request, identity).get(verification_id)
    except LookupError:
        raise not_found("Native verification not found") from None
    if result["pipeline_id"] != pipeline_id:
        raise not_found("Native verification not found")
    return result


@router.post("/{pipeline_id}/native-verifications")
async def request_native_verification(
    pipeline_id: str, request: Request, response: Response, identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    path = f"/v2/pipelines/{pipeline_id}/native-verifications"
    try:
        before = _native_service(request, identity).get_latest(pipeline_id)
        with request.app.state.engine.connect() as connection:
            _, context, _, _ = context_snapshot(
                connection, request.app.state.org_id, pipeline_id, identity.source_restriction,
            )
    except LookupError:
        raise not_found("Pipeline not found") from None
    preparer_factory = request.app.state.native_verification_preparer_factory
    controller_factory = request.app.state.native_verification_controller_factory
    if (not context["organization"]["native_context_enforced"]
            or not callable(preparer_factory) or not callable(controller_factory)):
        raise capability_unavailable("Native verification collection and authority are unavailable.")
    try:
        preparer = preparer_factory(request.app.state.org_id)
        controller = controller_factory(request.app.state.org_id)
    except Exception:
        raise capability_unavailable("Native verification collection is unavailable.") from None
    if not callable(getattr(preparer, "prepare", None)) or not callable(getattr(controller, "admit", None)):
        raise capability_unavailable("Native verification collection is unavailable.")

    async def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        if set(payload) - {"dry_run"} or type(payload.get("dry_run", False)) is not bool:
            raise invalid_request("Only a boolean dry_run may be supplied; native proofs are collected internally.")
        if payload.get("dry_run", False):
            return 200, envelope(
                before=before, after=before, verify_method="GET", verify_path=path + "/latest",
                dry_run={"would_request": "native_verification"},
            )
        service = _native_service(request, identity, preparer=preparer, controller=controller)
        result = await asyncio.to_thread(
            _dispatch_native_verification, service, pipeline_id, requested_by=identity.subject,
            idempotency_key=request.headers["idempotency-key"],
        )
        return 201, envelope(
            before=before, after=result, verify_method="GET", verify_path=path + "/" + result["id"],
        )

    receipt = await idempotent_write(
        request=request, response=response, actor_id=identity.subject, method="POST", path=path,
        store=request.app.state.idempotency_store, build=build,
        audit=AuditContext(action="pipeline.native_verification.request", resource_type="pipeline",
                           resource_id=pipeline_id), identity=identity,
    )
    after = receipt.get("after")
    if isinstance(after, dict) and after.get("context_validity") == "same_context":
        try:
            current = _native_service(request, identity).get(after["id"])
        except (LookupError, KeyError):
            raise capability_unavailable("The native verification receipt is no longer current.") from None
        if current["context_validity"] != "same_context":
            raise capability_unavailable("The native verification receipt is no longer current.")
    return receipt


@router.get("")
async def list_pipelines(
    request: Request,
    state: str | None = None,
    source_id: str | None = None,
    destination_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    service = _service(request)
    try:
        records, next_cursor = service.list(
            state=state,
            source_id=source_id,
            source_ids=identity.source_restriction or None,
            destination_id=destination_id,
            limit=limit,
            cursor=cursor,
            observation_provider=_observation_provider(request),
        )
    except InvalidListFilterError as error:
        raise invalid_request(str(error)) from error
    return {"items": [record.to_dict() for record in records], "next_cursor": next_cursor}


@router.get("/{pipeline_id}")
async def get_pipeline(
    pipeline_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(pipeline_id)
    except PipelineNotFoundError as error:
        raise not_found("Pipeline not found") from error
    return record.to_dict()


_METRICS_WINDOWS = ("1h", "24h")


@router.get("/{pipeline_id}/evidence")
async def get_pipeline_evidence(
    pipeline_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    try:
        _service(request).get(pipeline_id)
    except PipelineNotFoundError as error:
        raise not_found("Pipeline not found") from error
    reader = getattr(_observation_provider(request), "evidence", None)
    result = reader(pipeline_id) if callable(reader) else absent_evidence(
        pipeline_id, reason="No delivery evidence provider configured for this table."
    )
    body = result.to_dict()
    # Stage details may quote loader output: same redaction as the logs route.
    for stage in ("reading", "delivery", "reconciliation"):
        if isinstance(body.get(stage), dict) and isinstance(body[stage].get("detail"), str):
            body[stage]["detail"] = redact_log_message(body[stage]["detail"])
    return body


@router.get("/{pipeline_id}/metrics")
async def get_pipeline_metrics(
    pipeline_id: str,
    request: Request,
    window: str = "1h",
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    if window not in _METRICS_WINDOWS:
        raise invalid_request("window must be '1h' or '24h'")
    service = _service(request)
    try:
        service.get(pipeline_id)  # 404 si le pipeline n'existe pas
    except PipelineNotFoundError as error:
        raise not_found("Pipeline not found") from error
    series = _observation_provider(request).metrics(pipeline_id, window)
    return series.to_dict()


@router.get("/{pipeline_id}/logs")
async def get_pipeline_logs(
    pipeline_id: str,
    request: Request,
    since: str | None = None,
    level: str | None = None,
    correlate_incident: bool = False,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    if level is not None and level not in KNOWN_LEVELS:
        raise invalid_request("level must be 'info', 'warning' or 'error'")
    service = _service(request)
    try:
        service.get(pipeline_id)  # 404 si le pipeline n'existe pas
    except PipelineNotFoundError as error:
        raise not_found("Pipeline not found") from error
    source = getattr(request.app.state, "log_source", None) or NullLogSource()
    entries = LogsService(source).fetch(
        pipeline_id, since=since, level=level, incident=correlate_incident
    )
    return {"items": [entry.to_dict() for entry in entries], "next_cursor": None}


@router.post("/{pipeline_id}/actions/{action}")
async def run_pipeline_action(
    pipeline_id: str,
    action: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    if action not in ACTIONS:
        raise not_found("unknown pipeline action in this scope")
    service = _service(request)
    path = f"/v2/pipelines/{pipeline_id}/actions/{action}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        action_ref = f"pipeline.{action}"
        confirmations = _confirmations_service(request)
        confirmation_id = payload.get("confirmation_token") if isinstance(payload.get("confirmation_token"), str) else None
        try:
            if dry_run:
                plan = service.plan_action(pipeline_id, action)
                return 200, envelope(
                    before=None,
                    after=None,
                    verify_method="GET",
                    verify_path=f"/v2/pipelines/{pipeline_id}",
                    dry_run=plan,
                )

            if action in CONFIRMATION_REQUIRED_ACTIONS:
                if confirmation_id is None:
                    service.get(pipeline_id)  # 404 si le pipeline n'existe pas
                    conf_record, _approval_token = confirmations.create(
                        action_ref=action_ref,
                        resource_type="pipeline",
                        resource_id=pipeline_id,
                        reason=f"action sensible « {action}' on pipeline {pipeline_id}",
                        requested_by_kind=identity.actor_kind,
                        requested_by_id=identity.actor_id,
                    )
                    raise pending_confirmation_required(
                        f"confirmation required (id={conf_record.id}) — see /v2/confirmations/{conf_record.id}"
                    )
                try:
                    confirmation = confirmations.get(confirmation_id)
                except ConfirmationNotFoundError as error:
                    raise wrong_confirmation("unknown confirmation_token") from error
                if (
                    confirmation.state != "approved"
                    or confirmation.resource_id != pipeline_id
                    or confirmation.action_ref != action_ref
                ):
                    raise wrong_confirmation(
                        f"confirmation {confirmation_id} absent, expired or not approved for this action"
                    )

            executor = getattr(request.app.state, "pipeline_executor", None)
            before, after = service.apply_action(pipeline_id, action, executor=executor)
        except PipelineNotFoundError as error:
            raise not_found("Pipeline not found") from error
        except (ForbiddenTransitionError, UnknownStateError) as error:
            raise capability_unavailable(str(error)) from error
        except PipelineExecutorUnavailableError as error:
            raise ApiError(
                503,
                "executor_unavailable",
                str(error),
                next_action="check the control plane deployment",
                retryable=True,
            ) from error

        if confirmation_id is not None and action in CONFIRMATION_REQUIRED_ACTIONS:
            confirmations.mark_used(confirmation_id)

        return 200, envelope(
            before=before.to_dict(),
            after=after.to_dict(),
            verify_method="GET",
            verify_path=f"/v2/pipelines/{pipeline_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action=f"pipeline.{action}", resource_type="pipeline", resource_id=pipeline_id),
        identity=identity,
    )
