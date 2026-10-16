"""Resolve a restricted token's source before route work or cached responses."""

from sqlalchemy import select

from .. import schema
from ..errors import not_found, wrong_environment


def _source_id(request, resource_type: str, resource_id: str) -> str:
    sources, tables, pipelines = schema.sources, schema.tables, schema.pipelines
    statement = select(sources.c.id).where(sources.c.org_id == request.app.state.org_id)
    if resource_type == "source":
        statement = statement.where(sources.c.id == resource_id)
    elif resource_type == "table":
        statement = statement.join(tables, tables.c.source_id == sources.c.id).where(tables.c.id == resource_id)
    elif resource_type == "pipeline":
        statement = statement.join(tables, tables.c.source_id == sources.c.id).join(
            pipelines, pipelines.c.table_id == tables.c.id,
        ).where(pipelines.c.id == resource_id)
    else:
        raise wrong_environment("This operation has no safe source scope for this token.")
    with request.app.state.engine.connect() as connection:
        value = connection.execute(statement).scalar_one_or_none()
    if value is None:
        raise not_found("Resource not found")
    return value


def _require_resource(request, identity, resource_type: str, resource_id: str) -> None:
    source_id = _source_id(request, resource_type, resource_id)
    if not identity.source_allowed(source_id):
        raise wrong_environment("This source is outside the token's declared scope.")


def _require_confirmation(request, identity, confirmation_id: str) -> None:
    with request.app.state.engine.connect() as connection:
        row = connection.execute(select(
            schema.confirmations.c.resource_type, schema.confirmations.c.resource_id,
        ).where(
            schema.confirmations.c.id == confirmation_id,
            schema.confirmations.c.org_id == request.app.state.org_id,
        )).first()
    if row is None:
        raise not_found("Confirmation not found")
    _require_resource(request, identity, row.resource_type, row.resource_id)


def enforce_source_access(request, identity) -> None:
    """Deny unscoped v2 operations; list routes apply the allowed source set."""
    if not identity.source_restriction:
        return
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if not route.startswith("/v2/"):
        return
    if route == "/v2/auth/me":
        return
    if request.method == "GET" and route in {"/v2/sources", "/v2/pipelines"}:
        requested_source = request.query_params.get("source_id")
        if route == "/v2/pipelines" and requested_source is not None:
            _require_resource(request, identity, "source", requested_source)
        return
    for prefix, parameter, resource_type in (
        ("/v2/sources/{source_id}", "source_id", "source"),
        ("/v2/tables/{table_id}", "table_id", "table"),
        ("/v2/pipelines/{pipeline_id}", "pipeline_id", "pipeline"),
    ):
        if route == prefix or route.startswith(prefix + "/"):
            _require_resource(request, identity, resource_type, request.path_params[parameter])
            return
    if route.startswith("/v2/confirmations/{confirmation_id}"):
        _require_confirmation(request, identity, request.path_params["confirmation_id"])
        return
    if route == "/v2/costs" and request.query_params.get("scope") == "table":
        table_id = request.query_params.get("id")
        if table_id:
            _require_resource(request, identity, "table", table_id)
            return
    raise wrong_environment("This operation has no safe source scope for this token.")

