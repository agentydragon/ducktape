"""Stable HTTP API with separate workload and operator/BFF authentication paths.

The one exception is `/v1/action-groups`: its response is identical and non-sensitive for both
callers (already fully agent-visible, so an operator gains nothing new by also reading it), so it
accepts either bearer scheme rather than being duplicated under `/v1/operator/...`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.routing import Route

from agentplane.action_service.auth import OperatorAuthenticator
from agentplane.action_service.caller_auth import CallerTokenVerifier
from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionGroupView,
    ActionUnavailableError,
    ActionView,
    UnknownActionError,
)
from agentplane.action_service.connections import (
    Connection,
    ConnectionAuthority,
    ConnectionConflictError,
    ConnectionNotFoundError,
    ConnectionRebind,
    ConnectionRename,
    ConnectionVersion,
    GrantRejectedError,
)
from agentplane.action_service.enrollments import (
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
from agentplane.action_service.mcp_frontend import DisconnectSignalMiddleware, create_server
from agentplane.action_service.mcp_linkage import (
    McpLinkageAuthority,
    McpLinkageConflictError,
    McpLinkageError,
    McpLinkageNotFoundError,
    McpLinkageStart,
    McpLinkageStartView,
    McpLinkageStatus,
    McpLinkageView,
)
from agentplane.action_service.models import (
    ActionEventView,
    ActionHistoryPage,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    CancellationResult,
    DecisionInput,
    OperatorPrincipal,
    ServiceReaderPrincipal,
)
from agentplane.action_service.oauth import ActionsOAuthProxy
from agentplane.action_service.operator_stream import snapshot_stream
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.policy_view import CallerActionPolicyView, SubjectActionPolicyView
from agentplane.action_service.push import PushIdentity, PushSubscriptionStore
from agentplane.action_service.service import (
    ActionService,
    InvalidActionArgumentsError,
    ServiceDrainingError,
    UnsupportedActionError,
)
from agentplane.action_service.store import ActionConflictError, ActionNotFoundError, ExternalGrantNotAuthorizedError
from agentplane.action_service.updates import ActionUpdateBroker
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

logger = logging.getLogger(__name__)


class PushSubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = Field(min_length=1, max_length=2048)
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class _Authenticators:
    workload: WorkloadPrincipalAuthenticator
    operator: OperatorAuthenticator
    callers: PolicyIndex
    reader_accounts: frozenset[ServiceAccountRef]


def _authenticators(request: Request) -> _Authenticators:
    return cast(_Authenticators, request.app.state.authenticators)


_operator_bearer = HTTPBearer(auto_error=False)
_Auth = Annotated[_Authenticators, Depends(_authenticators)]
_Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(_operator_bearer)]


IDEMPOTENCY_KEY_FILTER = (
    "Only the request submitted under this idempotency key; recovers a submission whose response was lost."
)


def _sse_json(rows: Iterable[BaseModel]) -> bytes:
    return json.dumps([row.model_dump(mode="json") for row in rows], separators=(",", ":")).encode()


def _mcp_group_snapshot(catalog: ActionCatalog) -> bytes:
    """The operator's complete MCP catalog view, including currently available Actions."""
    return _sse_json(row for row in catalog.group_views(with_detail=True) if row.executor_kind == "mcp")


async def _try_workload(request: Request, auth: _Authenticators) -> CallerPrincipal | None:
    try:
        principal = await auth.workload(request)
    except HTTPException:
        return None
    return auth.callers.admit(principal.account)


async def _workload(request: Request, auth: _Auth) -> CallerPrincipal:
    """The ServiceAccount the bearer proves, once the index says that account may call here.

    Authenticating is not being admitted: without the label an account reaches no route, so a
    workload the operator has not named cannot queue Actions for them either.
    """
    caller = await _try_workload(request, auth)
    if caller is None:
        # Deliberately the authenticator's own generic refusal: which account was presented is not
        # the caller's to learn from the difference.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"}
        )
    return caller


async def _reader(request: Request, auth: _Auth) -> CallerPrincipal | ServiceReaderPrincipal:
    workload = await auth.workload(request)
    if workload.account in auth.reader_accounts:
        return ServiceReaderPrincipal(account=workload.account)
    caller = auth.callers.admit(workload.account)
    if caller is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"}
        )
    return caller


async def _try_operator(
    credentials: HTTPAuthorizationCredentials | None, auth: _Authenticators
) -> OperatorPrincipal | None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return await auth.operator.authenticate(credentials.credentials)


async def _operator(credentials: _Bearer, auth: _Auth) -> OperatorPrincipal:
    principal = await _try_operator(credentials, auth)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "operator bearer required", headers={"WWW-Authenticate": "Bearer"}
        )
    return principal


async def _workload_or_operator(
    request: Request, credentials: _Bearer, auth: _Auth
) -> CallerPrincipal | OperatorPrincipal:
    """Either bearer scheme, for the one route both callers read."""
    principal = await _try_workload(request, auth) or await _try_operator(credentials, auth)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "workload or operator bearer required", headers={"WWW-Authenticate": "Bearer"}
        )
    return principal


_Workload = Annotated[CallerPrincipal, Depends(_workload)]
_Reader = Annotated[CallerPrincipal | ServiceReaderPrincipal, Depends(_reader)]
_Operator = Annotated[OperatorPrincipal, Depends(_operator)]


async def _stream_authorization(
    principal: _Operator, credentials: _Bearer, auth: _Auth
) -> Callable[[], Awaitable[bool]]:
    """Recheck stream authorization on every snapshot and keepalive, not just at connect time."""

    async def authorized() -> bool:
        return credentials is not None and await auth.operator.authenticate(credentials.credentials) == principal

    return authorized


_StreamAuthorized = Annotated[Callable[[], Awaitable[bool]], Depends(_stream_authorization)]

# Each detail is a fixed string or derived from the error. Starlette picks the handler of the most
# specific registered class, and `_handle_error` resolves the same way.
_ERRORS: dict[type[Exception], tuple[int, str | Callable[[Any], str]]] = {
    ActionNotFoundError: (status.HTTP_404_NOT_FOUND, "action request not found"),
    ActionUnavailableError: (status.HTTP_503_SERVICE_UNAVAILABLE, "ActionGroup is temporarily unavailable"),
    UnknownActionError: (
        status.HTTP_404_NOT_FOUND,
        lambda error: f"unknown group/action {(error.group_key, error.action_key)!r}",
    ),
    ActionConflictError: (status.HTTP_409_CONFLICT, str),
    ExternalGrantNotAuthorizedError: (status.HTTP_403_FORBIDDEN, "external grant is not authorized"),
    UnsupportedActionError: (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        lambda error: f"unsupported group/action {error.args[0]!r}",
    ),
    InvalidActionArgumentsError: (status.HTTP_422_UNPROCESSABLE_CONTENT, str),
    ServiceDrainingError: (status.HTTP_503_SERVICE_UNAVAILABLE, "Action Service is draining"),
    ConnectionNotFoundError: (status.HTTP_404_NOT_FOUND, "Connection not found"),
    ConnectionConflictError: (status.HTTP_409_CONFLICT, str),
    GrantRejectedError: (status.HTTP_400_BAD_REQUEST, str),
    EnrollmentRejectedError: (status.HTTP_403_FORBIDDEN, str),
    EnrollmentNotFoundError: (status.HTTP_404_NOT_FOUND, str),
    EnrollmentExpiredError: (status.HTTP_410_GONE, str),
    EnrollmentConflictError: (status.HTTP_409_CONFLICT, str),
    # A provider that refused or could not answer the token exchange; the subclasses keep their own.
    McpLinkageError: (status.HTTP_502_BAD_GATEWAY, str),
    McpLinkageConflictError: (status.HTTP_409_CONFLICT, str),
    McpLinkageNotFoundError: (status.HTTP_404_NOT_FOUND, str),
}


async def _handle_error(request: Request, error: Exception) -> Response:
    code, detail = next(_ERRORS[kind] for kind in type(error).__mro__ if kind in _ERRORS)
    return _error(code, detail if isinstance(detail, str) else detail(error))


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def create_app(
    service: ActionService,
    workload_resolver: WorkloadPrincipalResolver,
    operator_authenticator: OperatorAuthenticator,
    catalog: ActionCatalog,
    *,
    callers: PolicyIndex,
    updates: ActionUpdateBroker,
    connections: ConnectionAuthority | None = None,
    enrollments: EnrollmentAuthority | None = None,
    oauth: ActionsOAuthProxy | None = None,
    push_identity: PushIdentity | None = None,
    push_subscriptions: PushSubscriptionStore | None = None,
    mcp_linkage: McpLinkageAuthority | None = None,
    direct_wait_seconds: float,
    max_wait_seconds: float,
    reader_accounts: frozenset[ServiceAccountRef] = frozenset(),
) -> FastAPI:
    verifier = CallerTokenVerifier(workload_resolver, callers=callers, oauth=oauth)
    mcp_app = create_server(
        service, catalog, updates, verifier, direct_wait_seconds=direct_wait_seconds, max_wait_seconds=max_wait_seconds
    ).http_app(path="/mcp", stateless_http=True, json_response=False, host_origin_protection="auto")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with updates.listener.listen(), mcp_app.lifespan(mcp_app):
            yield

    app = FastAPI(title="Agentplane Action Service", version="v1", lifespan=lifespan)
    app.state.action_service = service
    app.state.action_catalog = catalog
    app.state.authenticators = _Authenticators(
        workload=WorkloadPrincipalAuthenticator(workload_resolver),
        operator=operator_authenticator,
        callers=callers,
        reader_accounts=reader_accounts,
    )
    for error_type in _ERRORS:
        app.add_exception_handler(error_type, _handle_error)
    # Operator/BFF surface: deliberately different paths and authenticator. A workload bearer can
    # never acquire operator-all read or decision authority merely by authenticating as a Sandbox.
    operator = APIRouter(prefix="/v1/operator", dependencies=[Depends(_operator)])

    if connections is not None:
        _connection_routes(operator, connections, updates)
    if enrollments is not None:
        _enrollment_routes(operator, enrollments)
    if mcp_linkage is not None:
        _mcp_linkage_routes(app, operator, mcp_linkage, updates)

    @app.middleware("http")
    async def drain_admission(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if service.draining and request.url.path not in {"/healthz", "/metrics"}:
            return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Action Service is draining")
        return await call_next(request)

    @app.get("/readyz")
    async def readyz() -> Response:
        """Ready means the policy index is complete and still moving, not merely that the process serves.

        A replica whose watches have wedged auto-decides from a frozen snapshot -- the wrong
        direction for a policy engine to be wrong in -- so it leaves the Service here rather than
        going on answering. The ages are the facts; the status code is the verdict on them.
        """
        now = callers.clock()
        return JSONResponse(
            {
                "synced": callers.synced,
                "listed": callers.listed,
                "staleAfterSeconds": callers.freshness.stale_after_seconds,
                "refreshedSecondsAgo": {kind: round(age, 1) for kind, age in callers.freshness.ages(now).items()},
            },
            status_code=status.HTTP_200_OK if callers.synced else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Workload surface: every endpoint resolves an ordinary Authorization bearer through the same
    # shared workload path the MCP surface uses. No operator adapter is consulted here.
    @app.post("/v1/action-requests", status_code=status.HTTP_202_ACCEPTED)
    async def submit(body: ActionRequestInput, principal: _Workload) -> ActionRequestView:
        return await service.submit(body, principal)

    @app.get("/v1/action-requests")
    async def list_own_requests(
        principal: _Reader,
        state_filter: Annotated[list[ActionState] | None, Query(alias="state")] = None,
        idempotency_key: Annotated[str | None, Query(description=IDEMPOTENCY_KEY_FILTER)] = None,
    ) -> list[ActionRequestView]:
        return await service.list_requests(principal, states=tuple(state_filter or ()), idempotency_key=idempotency_key)

    @app.get("/v1/action-requests/{request_id}")
    async def get_own_request(request_id: UUID, principal: _Reader) -> ActionRequestView:
        return await service.get(request_id, principal)

    @app.post("/v1/action-requests/{request_id}/cancel")
    async def cancel_own_request(request_id: UUID, principal: _Workload) -> CancellationResult:
        return await service.cancel(request_id, principal)

    @app.get("/v1/action-requests/{request_id}/events")
    async def own_events(
        request_id: UUID,
        principal: _Reader,
        after_sequence: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
    ) -> list[ActionEventView]:
        return await service.events(request_id, principal, after_sequence=after_sequence, limit=limit)

    # Catalog discovery: the reviewed, config-driven ActionGroup/Action universe. Read-only, and the
    # same for every caller, so it carries no owner-scoping unlike the ActionRequest surface above.
    # Operators read this too, for the MCP servers settings page; theirs alone keeps each group's
    # health detail, which can carry backend exception text.
    @app.get("/v1/action-groups")
    async def list_action_groups(
        principal: Annotated[CallerPrincipal | OperatorPrincipal, Depends(_workload_or_operator)],
    ) -> list[ActionGroupView]:
        return catalog.group_views(with_detail=isinstance(principal, OperatorPrincipal))

    @operator.get("/action-groups/stream")
    async def group_health_stream(authorized: _StreamAuthorized) -> StreamingResponse:
        async def read() -> bytes:
            return _mcp_group_snapshot(catalog)

        return snapshot_stream(updates.subscribe_mcp_health, read, authorized)

    @app.get("/v1/action-groups/{group_key}/actions/{action_key}", dependencies=[Depends(_workload)])
    async def get_action(group_key: str, action_key: str) -> ActionView:
        return catalog.action_view(group_key, action_key)

    @app.get("/v1/action-policy")
    async def own_action_policy(principal: _Workload) -> CallerActionPolicyView:
        """The caller's own effective policy, from the resolution admission uses: the bindings on it, the
        sets that resolved, and the auto_approve_if entries in evaluation order. This surface only
        ever sees a Sandbox principal; an external grant reaches the service through `/mcp`, whose
        tool reads the grant `CallerTokenVerifier` verified."""
        return service.caller_action_policy(principal, external_grant=None)

    @operator.get("/action-requests")
    async def operator_list_requests(
        principal: _Operator,
        state_filter: Annotated[list[ActionState] | None, Query(alias="state")] = None,
        idempotency_key: Annotated[str | None, Query(description=IDEMPOTENCY_KEY_FILTER)] = None,
    ) -> list[ActionRequestView]:
        return await service.list_requests(principal, states=tuple(state_filter or ()), idempotency_key=idempotency_key)

    @operator.get("/action-requests/history")
    async def operator_history(
        principal: _Operator, limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = None
    ) -> ActionHistoryPage:
        try:
            items, next_cursor = await service.history(principal, limit=limit, cursor=cursor)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return ActionHistoryPage(items=items, next_cursor=next_cursor)

    @operator.get("/action-requests/stream")
    async def operator_stream(
        principal: _Operator, authorized: _StreamAuthorized, state: Annotated[ActionState | None, Query()] = None
    ) -> StreamingResponse:
        async def read() -> bytes:
            return _sse_json(await service.list_requests(principal, states=(state,) if state else ()))

        return snapshot_stream(updates.subscribe_all, read, authorized, changed_event=True)

    @operator.get("/action-requests/{request_id}")
    async def operator_get_request(request_id: UUID, principal: _Operator) -> ActionRequestView:
        return await service.get(request_id, principal)

    @operator.get("/action-policy/service-accounts/{namespace}/{name}")
    async def operator_service_account_action_policy(namespace: str, name: str) -> SubjectActionPolicyView:
        """A subject's effective policy as the operator sees it: each binding with its labels and
        Ready verdict, each named set as present, refused or missing, and the resolved entries."""
        return service.subject_action_policy(ServiceAccountRef(namespace=namespace, name=name))

    @operator.get("/push/config")
    async def push_config() -> dict[str, str | None]:
        return {"application_server_key": push_identity.application_server_key if push_identity else None}

    @operator.get("/push/subscriptions")
    async def list_push_subscriptions(principal: _Operator) -> list[dict[str, object]]:
        if push_subscriptions is None:
            return []
        return [
            {"endpoint": row.endpoint, "user_agent": row.user_agent, "created_at": row.created_at.isoformat()}
            for row in await push_subscriptions.list_for(principal)
        ]

    @operator.get("/push/subscriptions/stream")
    async def push_subscriptions_stream(principal: _Operator, authorized: _StreamAuthorized) -> StreamingResponse:
        async def read() -> bytes:
            return json.dumps(await list_push_subscriptions(principal), separators=(",", ":")).encode()

        return snapshot_stream(updates.subscribe_push, read, authorized)

    @operator.post("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
    async def register_push_subscription(body: PushSubscriptionInput, principal: _Operator, request: Request) -> None:
        if push_identity is None or push_subscriptions is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "web push is not configured")
        try:
            push_identity.validate_endpoint(body.endpoint)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported push endpoint") from None
        user_agent = request.headers.get("user-agent")
        try:
            await push_subscriptions.save(
                operator=principal,
                endpoint=body.endpoint,
                p256dh=body.p256dh,
                auth=body.auth,
                user_agent=user_agent[:300] if user_agent else None,
            )
        except ValueError:
            raise HTTPException(status.HTTP_409_CONFLICT, "subscription is already registered") from None

    @operator.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
    async def remove_push_subscription(endpoint: str, principal: _Operator) -> None:
        if push_subscriptions is None or not await push_subscriptions.delete(operator=principal, endpoint=endpoint):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such push subscription")

    @operator.post("/action-requests/{request_id}/decision")
    async def decide(request_id: UUID, body: DecisionInput, principal: _Operator) -> ActionRequestView:
        return await service.decide(request_id, body, principal)

    app.include_router(operator)
    # Match only the transport endpoint, without a slash redirect or intercepting unknown REST paths.
    if oauth is not None:
        app.router.routes.extend(oauth.get_routes(mcp_path="/mcp"))
    app.router.routes.append(Route("/mcp", DisconnectSignalMiddleware(mcp_app)))
    return app


def _connection_routes(operator: APIRouter, authority: ConnectionAuthority, updates: ActionUpdateBroker) -> None:
    @operator.get("/caller-service-accounts")
    async def caller_service_accounts() -> list[ServiceAccountRef]:
        return authority.caller_service_accounts()

    @operator.get("/connections")
    async def connections() -> list[Connection]:
        return await authority.list()

    @operator.get("/connections/stream")
    async def connections_stream(authorized: _StreamAuthorized) -> StreamingResponse:
        async def read() -> bytes:
            return _sse_json(await authority.list())

        return snapshot_stream(updates.subscribe_connections, read, authorized)

    @operator.get("/connections/{connection_id}")
    async def connection(connection_id: UUID) -> Connection:
        return await authority.get(connection_id)

    @operator.patch("/connections/{connection_id}")
    async def rename_connection(connection_id: UUID, body: ConnectionRename) -> Connection:
        return await authority.rename(
            connection_id, expected_version=body.expected_version, display_name=body.display_name
        )

    @operator.post("/connections/{connection_id}/rebind")
    async def rebind_connection(connection_id: UUID, body: ConnectionRebind) -> Connection:
        return await authority.rebind(
            connection_id, expected_version=body.expected_version, caller=body.service_account
        )

    @operator.post("/connections/{connection_id}/unbind")
    async def unbind_connection(connection_id: UUID, body: ConnectionVersion) -> Connection:
        return await authority.unbind(connection_id, expected_version=body.expected_version)


def _enrollment_routes(operator: APIRouter, authority: EnrollmentAuthority) -> None:
    @operator.post("/connection-enrollments/{handle}/preview")
    async def enrollment_preview(handle: str, body: EnrollmentPreviewInput, principal: _Operator) -> EnrollmentPreview:
        return await authority.preview(handle, body, principal)

    @operator.post("/connection-enrollments/{handle}/decision")
    async def enrollment_decision(
        handle: str, body: EnrollmentDecisionInput, principal: _Operator
    ) -> EnrollmentDecisionResult:
        return await authority.decide(handle, body, principal)


def _mcp_linkage_routes(
    app: FastAPI, operator: APIRouter, authority: McpLinkageAuthority, updates: ActionUpdateBroker
) -> None:
    @app.get("/oauth/client-metadata.json")
    async def client_metadata_document() -> JSONResponse:
        document = authority.client_metadata_document()
        return JSONResponse(
            content=document.model_dump(mode="json", exclude_none=True),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @operator.get("/mcp-servers/{server_id}/linkage")
    async def linkage_status(server_id: str) -> McpLinkageView:
        return await authority.status(server_id)

    @operator.get("/mcp-servers")
    async def list_mcp_linkages() -> list[McpLinkageView]:
        return await authority.statuses()

    @operator.get("/mcp-servers/stream")
    async def mcp_linkages_stream(authorized: _StreamAuthorized) -> StreamingResponse:
        next_expiry: datetime | None = None

        async def read() -> bytes:
            nonlocal next_expiry
            rows = await authority.statuses()
            now = datetime.now(UTC)
            next_expiry = min(
                (
                    row.expires_at
                    for row in rows
                    if row.status == McpLinkageStatus.LINKED and row.expires_at is not None and row.expires_at > now
                ),
                default=None,
            )
            return _sse_json(rows)

        return snapshot_stream(updates.subscribe_mcp_linkages, read, authorized, refresh_at=lambda: next_expiry)

    @operator.post("/mcp-servers/{server_id}/linkage/start")
    async def linkage_start(server_id: str, body: McpLinkageStart, principal: _Operator) -> McpLinkageStartView:
        return await authority.start(server_id, body, principal)

    @app.get("/v1/mcp-linkage/callback")
    async def linkage_callback(state: str, code: str) -> McpLinkageView:
        return await authority.callback(state, code)

    @operator.post("/mcp-servers/{server_id}/linkage/disconnect")
    async def linkage_disconnect(server_id: str, principal: _Operator) -> McpLinkageView:
        return await authority.disconnect(server_id, principal)
