"""Stable HTTP API with separate workload and operator/BFF authentication paths."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from x.agentplane.action_service.auth import OperatorAuthenticator, workload_principal
from x.agentplane.action_service.db import ActionConflictError, ActionNotFoundError, UnknownCapabilityError
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionInput,
    Principal,
    PrincipalRole,
)
from x.agentplane.action_service.service import ActionService
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

_operator_bearer = HTTPBearer(auto_error=False)


def _service(request: Request) -> ActionService:
    return cast(ActionService, request.app.state.action_service)


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
) -> FastAPI:
    app = FastAPI(title="Agentplane Action Service", version="v1")
    app.state.action_service = service
    app.state.workload_authenticator = workload_authenticator
    app.state.operator_authenticator = operator_authenticator

    @app.exception_handler(ActionNotFoundError)
    async def not_found(request: Request, error: ActionNotFoundError) -> JSONResponse:
        del request, error
        return _error(status.HTTP_404_NOT_FOUND, "action request not found")

    @app.exception_handler(ActionConflictError)
    async def conflict(request: Request, error: ActionConflictError) -> JSONResponse:
        del request
        return _error(status.HTTP_409_CONFLICT, str(error))

    @app.exception_handler(UnknownCapabilityError)
    async def unsupported(request: Request, error: UnknownCapabilityError) -> JSONResponse:
        del request
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported capability {error.args[0]!r}")

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

    @app.get("/v1/action-requests/{request_id}/events", response_model=list[ActionEventView])
    async def own_events(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_workload)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> list[ActionEventView]:
        return await action_service.events(request_id, principal)

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
    ) -> list[ActionEventView]:
        return await action_service.events(request_id, principal)

    @app.post("/v1/operator/action-requests/{request_id}/decision", response_model=ActionRequestView)
    async def decide(
        request_id: UUID,
        body: DecisionInput,
        principal: Annotated[Principal, Depends(_operator)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.decide(request_id, body, principal)

    return app


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})
