"""Stable versioned HTTP API for the standalone Action Service."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from x.agentplane.action_service.auth import Authenticator
from x.agentplane.action_service.db import ActionConflictError, ActionNotFoundError, UnknownCapabilityError
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionInput,
    Principal,
)
from x.agentplane.action_service.service import ActionService

_bearer = HTTPBearer(auto_error=False)


def _service(request: Request) -> ActionService:
    return cast(ActionService, request.app.state.action_service)


def _authenticator(request: Request) -> Authenticator:
    return cast(Authenticator, request.app.state.authenticator)


async def _principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    authenticator: Annotated[Authenticator, Depends(_authenticator)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
    principal = await authenticator.authenticate(credentials.credentials)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token is not accepted")
    return principal


def create_app(service: ActionService, authenticator: Authenticator) -> FastAPI:
    app = FastAPI(title="Agentplane Action Service", version="v1")
    app.state.action_service = service
    app.state.authenticator = authenticator

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

    @app.post("/v1/action-requests", response_model=ActionRequestView, status_code=status.HTTP_202_ACCEPTED)
    async def submit(
        body: ActionRequestInput,
        principal: Annotated[Principal, Depends(_principal)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.submit(body, principal)

    @app.get("/v1/action-requests", response_model=list[ActionRequestView])
    async def list_requests(
        principal: Annotated[Principal, Depends(_principal)],
        action_service: Annotated[ActionService, Depends(_service)],
        state_filter: Annotated[list[ActionState] | None, Query(alias="state")] = None,
    ) -> list[ActionRequestView]:
        return await action_service.list_requests(principal, states=tuple(state_filter or ()))

    @app.get("/v1/action-requests/{request_id}", response_model=ActionRequestView)
    async def get_request(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_principal)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.get(request_id, principal)

    @app.get("/v1/action-requests/{request_id}/events", response_model=list[ActionEventView])
    async def events(
        request_id: UUID,
        principal: Annotated[Principal, Depends(_principal)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> list[ActionEventView]:
        return await action_service.events(request_id, principal)

    @app.post("/v1/action-requests/{request_id}/decision", response_model=ActionRequestView)
    async def decide(
        request_id: UUID,
        body: DecisionInput,
        principal: Annotated[Principal, Depends(_principal)],
        action_service: Annotated[ActionService, Depends(_service)],
    ) -> ActionRequestView:
        return await action_service.decide(request_id, body, principal)

    return app


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})
