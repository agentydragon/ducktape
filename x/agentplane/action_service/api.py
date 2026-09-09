"""Stable HTTP API with separate workload and operator/BFF authentication paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.routing import Route

from x.agentplane.action_service.auth import OperatorAuthenticator, workload_principal
from x.agentplane.action_service.caller_auth import CallerAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroupView, ActionView, UnknownActionError
from x.agentplane.action_service.connections import (
    Connection,
    ConnectionAuthority,
    ConnectionConflictError,
    ConnectionName,
    ConnectionNotFoundError,
    Identity,
)
from x.agentplane.action_service.db import ActionConflictError, ActionNotFoundError, ExternalGrantNotAuthorizedError
from x.agentplane.action_service.enrollments import (
    EnrollmentAuthority,
    EnrollmentConflictError,
    EnrollmentDecisionInput,
    EnrollmentDecisionResult,
    EnrollmentExpiredError,
    EnrollmentNotFoundError,
    EnrollmentPreview,
    EnrollmentPreviewInput,
    EnrollmentRejectedError,
)
from x.agentplane.action_service.mcp_frontend import ActionsMcp, create_server
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CancellationResult,
    DecisionInput,
    Principal,
    PrincipalRole,
)
from x.agentplane.action_service.oauth import ActionsOAuthProxy
from x.agentplane.action_service.service import ActionService, InvalidActionArgumentsError, UnsupportedActionError
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

_operator_bearer = HTTPBearer(auto_error=False)


class ConnectionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class ConnectionRename(ConnectionVersion):
    display_name: ConnectionName


def _service(request: Request) -> ActionService:
    return cast(ActionService, request.app.state.action_service)


def _catalog(request: Request) -> ActionCatalog:
    return cast(ActionCatalog, request.app.state.action_catalog)


def _workload_authenticator(request: Request) -> SandboxPrincipalAuthenticator:
    return cast(SandboxPrincipalAuthenticator, request.app.state.workload_authenticator)


def _operator_authenticator(request: Request) -> OperatorAuthenticator:
    return cast(OperatorAuthenticator, request.app.state.operator_authenticator)


async def _workload(
    request: Request, authenticator: Annotated[SandboxPrincipalAuthenticator, Depends(_workload_authenticator)]
) -> Principal:
    return workload_principal(await authenticator(request))


async def _operator(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_operator_bearer)],
    authenticator: Annotated[OperatorAuthenticator, Depends(_operator_authenticator)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "operator bearer required", headers={"WWW-Authenticate": "Bearer"}
        )
    principal = await authenticator.authenticate(credentials.credentials)
    if principal is None or principal.role is not PrincipalRole.OPERATOR:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "operator bearer is not accepted", headers={"WWW-Authenticate": "Bearer"}
        )
    return principal


def create_app(
    service: ActionService,
    workload_authenticator: SandboxPrincipalAuthenticator,
    operator_authenticator: OperatorAuthenticator,
    catalog: ActionCatalog,
    *,
    updates: ActionUpdates,
    connections: ConnectionAuthority | None = None,
    enrollments: EnrollmentAuthority | None = None,
    oauth: ActionsOAuthProxy | None = None,
) -> FastAPI:
    caller_authenticator = CallerAuthenticator(workload_authenticator, oauth)
    mcp_app = create_server(service, catalog, updates, caller_authenticator).http_app(
        path="/mcp", stateless_http=True, json_response=False, host_origin_protection="auto"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await updates.start()
        try:
            async with mcp_app.lifespan(mcp_app):
                yield
        finally:
            await updates.close()

    app = FastAPI(title="Agentplane Action Service", version="v1", lifespan=lifespan)
    app.state.action_service = service
    app.state.workload_authenticator = workload_authenticator
    app.state.operator_authenticator = operator_authenticator
    app.state.action_catalog = catalog

    if connections is not None:
        _connection_routes(app, connections)
    if enrollments is not None:
        _enrollment_routes(app, enrollments)

    @app.exception_handler(ActionNotFoundError)
    async def not_found(request: Request, error: ActionNotFoundError) -> JSONResponse:
        del request, error
        return _error(status.HTTP_404_NOT_FOUND, "action request not found")

    @app.exception_handler(UnknownActionError)
    async def unknown_action(request: Request, error: UnknownActionError) -> JSONResponse:
        del request
        return _error(status.HTTP_404_NOT_FOUND, f"unknown group/action {(error.group_key, error.action_key)!r}")

    @app.exception_handler(ActionConflictError)
    async def conflict(request: Request, error: ActionConflictError) -> JSONResponse:
        del request
        return _error(status.HTTP_409_CONFLICT, str(error))

    @app.exception_handler(ExternalGrantNotAuthorizedError)
    async def external_grant_rejected(request: Request, error: ExternalGrantNotAuthorizedError) -> JSONResponse:
        del request, error
        return _error(status.HTTP_403_FORBIDDEN, "external grant is not authorized")

    @app.exception_handler(UnsupportedActionError)
    async def unsupported(request: Request, error: UnsupportedActionError) -> JSONResponse:
        del request
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported group/action {error.args[0]!r}")

    @app.exception_handler(InvalidActionArgumentsError)
    async def invalid_arguments(request: Request, error: InvalidActionArgumentsError) -> JSONResponse:
        del request
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Workload surface: every endpoint resolves an ordinary Authorization bearer through the
    # shared destination-side SandboxPrincipal path. No operator adapter is consulted here.
    @app.post("/v1/action-requests", response_model=ActionRequestView, status_code=status.HTTP_202_ACCEPTED)
    async def submit(
        body: ActionRequestInput,
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.submit(body, principal)

    @app.get("/v1/action-requests", response_model=list[ActionRequestView])
    async def list_own_requests(
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
        state_filter: Annotated[list[ActionState] | None, Query(alias="state")] = None,
    ) -> list[ActionRequestView]:
        return await action_service.list_requests(principal, states=tuple(state_filter or ()))

    @app.get("/v1/action-requests/{request_id}", response_model=ActionRequestView)
    async def get_own_request(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.get(request_id, principal)

    @app.post("/v1/action-requests/{request_id}/cancel", response_model=CancellationResult)
    async def cancel_own_request(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> CancellationResult:
        return await action_service.cancel(request_id, principal)

    @app.get("/v1/action-requests/{request_id}/events", response_model=list[ActionEventView])
    async def own_events(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ActionEventView]:
        return await action_service.events(request_id, principal, after_sequence=after_sequence)

    # Catalog discovery: the reviewed, config-driven ActionGroup/Action universe. Read-only, and the
    # same for every caller, so it carries no owner-scoping unlike the ActionRequest surface above.
    @app.get("/v1/action-groups", response_model=list[ActionGroupView])
    async def list_action_groups(
        principal: Annotated[Principal, Depends(_workload)], action_catalog: Annotated[ActionCatalog, Depends(_catalog)]
    ) -> list[ActionGroupView]:
        del principal
        return action_catalog.group_views()

    @app.get("/v1/action-groups/{group_key}/actions/{action_key}", response_model=ActionView)
    async def get_action(
        group_key: str,
        action_key: str,
        principal: Annotated[Principal, Depends(_workload)],
        action_catalog: Annotated[ActionCatalog, Depends(_catalog)],
    ) -> ActionView:
        del principal
        return action_catalog.action_view(group_key, action_key)

    # Operator/BFF surface: deliberately different paths and authenticator. A workload bearer can
    # never acquire operator-all read or decision authority merely by authenticating as a Sandbox.
    @app.get("/v1/operator/action-requests", response_model=list[ActionRequestView])
    async def operator_list_requests(
        principal: Annotated[Principal, Depends(_operator)],
        action_service: Annotated[ActionService, Depends(_service)],
        state_filter: Annotated[list[ActionState] | None, Query(alias="state")] = None,
    ) -> list[ActionRequestView]:
        return await action_service.list_requests(principal, states=tuple(state_filter or ()))

    @app.get("/v1/operator/action-requests/{request_id}", response_model=ActionRequestView)
    async def operator_get_request(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_operator)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.get(request_id, principal)

    @app.get("/v1/operator/action-requests/{request_id}/events", response_model=list[ActionEventView])
    async def operator_events(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_operator)],
        action_service: Annotated[ActionService, Depends(_service)],
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ActionEventView]:
        return await action_service.events(request_id, principal, after_sequence=after_sequence)

    @app.post("/v1/operator/action-requests/{request_id}/decision", response_model=ActionRequestView)
    async def decide(
        request_id: UUID,
        body: DecisionInput,
        principal: Annotated[Principal, Depends(_operator)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.decide(request_id, body, principal)

    # Match only the transport endpoint, without a slash redirect or intercepting unknown REST paths.
    if oauth is not None:
        app.router.routes.extend(oauth.get_routes(mcp_path="/mcp"))
    app.router.routes.append(Route("/mcp", ActionsMcp(mcp_app, caller_authenticator)))
    return app


def _connection_routes(app: FastAPI, authority: ConnectionAuthority) -> None:
    @app.exception_handler(ConnectionNotFoundError)
    async def connection_not_found(request: Request, error: ConnectionNotFoundError) -> JSONResponse:
        del request, error
        return _error(status.HTTP_404_NOT_FOUND, "Connection not found")

    @app.exception_handler(ConnectionConflictError)
    async def connection_conflict(request: Request, error: ConnectionConflictError) -> JSONResponse:
        del request
        return _error(status.HTTP_409_CONFLICT, str(error))

    @app.get("/v1/operator/identities", dependencies=[Depends(_operator)])
    async def identities() -> dict[str, Identity]:
        return authority.identities()

    @app.get("/v1/operator/connections", dependencies=[Depends(_operator)])
    async def connections() -> list[Connection]:
        return await authority.list()

    @app.get("/v1/operator/connections/{connection_id}", dependencies=[Depends(_operator)])
    async def connection(connection_id: UUID) -> Connection:
        return await authority.get(connection_id)

    @app.patch("/v1/operator/connections/{connection_id}", dependencies=[Depends(_operator)])
    async def rename_connection(connection_id: UUID, body: ConnectionRename) -> Connection:
        return await authority.rename(
            connection_id, expected_version=body.expected_version, display_name=body.display_name
        )

    @app.post("/v1/operator/connections/{connection_id}/unbind", dependencies=[Depends(_operator)])
    async def unbind_connection(connection_id: UUID, body: ConnectionVersion) -> Connection:
        return await authority.unbind(connection_id, expected_version=body.expected_version)


def _enrollment_routes(app: FastAPI, authority: EnrollmentAuthority) -> None:
    @app.exception_handler(EnrollmentRejectedError)
    async def enrollment_rejected(request: Request, error: EnrollmentRejectedError) -> JSONResponse:
        del request
        match error:
            case EnrollmentNotFoundError():
                code = status.HTTP_404_NOT_FOUND
            case EnrollmentExpiredError():
                code = status.HTTP_410_GONE
            case EnrollmentConflictError():
                code = status.HTTP_409_CONFLICT
            case _:
                code = status.HTTP_403_FORBIDDEN
        return _error(code, str(error))

    @app.post("/v1/operator/connection-enrollments/{handle}/preview")
    async def enrollment_preview(
        handle: str, body: EnrollmentPreviewInput, principal: Annotated[Principal, Depends(_operator)]
    ) -> EnrollmentPreview:
        return await authority.preview(handle, body, principal)

    @app.post("/v1/operator/connection-enrollments/{handle}/decision")
    async def enrollment_decision(
        handle: str, body: EnrollmentDecisionInput, principal: Annotated[Principal, Depends(_operator)]
    ) -> EnrollmentDecisionResult:
        return await authority.decide(handle, body, principal)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})
