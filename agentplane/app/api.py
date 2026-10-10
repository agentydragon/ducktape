"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, nullcontext
from pathlib import Path as FilePath
from typing import Annotated
from uuid import UUID

import grpc
import httpx
import httpx2
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from agentplane.action_service.catalog import ActionGroupView
from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.connections import Connection, ConnectionRebind, ConnectionRename, ConnectionVersion
from agentplane.action_service.enrollments import EnrollmentDecisionResult
from agentplane.action_service.mcp_linkage import McpLinkageStart, McpLinkageStartView, McpLinkageView
from agentplane.action_service.models import (
    ActionEventView,
    ActionHistoryPage,
    ActionRequestView,
    ActionState,
    DecisionInput,
)
from agentplane.action_service.policy_view import ActionPolicySetView
from agentplane.app import auth_routes
from agentplane.app.action_federation import (
    FederatedOperatorActions,
    OperatorFederationError,
    operator_actions,
    upstream_failure_detail,
)
from agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
from agentplane.app.consent import (
    ConsentDecision,
    ConsentPreview,
    EnrollmentHandle,
    decide_enrollment,
    preview_enrollment,
)
from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.app.decisions import Decision, DecisionsClient, DecisionsUnavailableError
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy, router as electric_router
from agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer, require_caller
from agentplane.app.live import LiveIndex, Updates, action_policy_frame, router as live_router
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.oidc import OIDCSettings, build_oauth
from agentplane.app.operator_sessions import (
    OperatorSessionMiddleware,
    OperatorSessionStore,
    operator_session_row,
    request_session,
)
from agentplane.app.presets import PresetCatalog, SandboxPresetView
from agentplane.app.sandbox_models import (
    KubernetesGrantView,
    NewSandbox,
    SandboxView,
    create_request,
    grant_views,
    sandbox_has_ready_pod,
    sandbox_view,
)
from agentplane.app.shutdown import Drain, DrainMiddleware, Shutdown, until_done
from agentplane.app.threads import bridge as runner_bridge
from agentplane.app.threads.events import stream
from agentplane.app.threads.events.debug import (
    ArchivedObservationEntry,
    EvidencePage,
    NativeFramePage,
    ObservationPage,
    ThreadEvidenceNotFoundError,
    ThreadScopeChangedError,
)
from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.sessions import SandboxNotReachableError
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import CommandIdConflictError, ContentStore, ThreadScopeResetError
from agentplane.app.threads.view.fold import CommandOutcome
from agentplane.app.threads.view.views import ThreadView
from agentplane.history_service.client import HistoryServiceError
from agentplane.notification_service.models import SandboxNotificationStatus
from agentplane.runner import protocol_pb2
from agentplane.runner.errors import OpenTimeoutError, RunnerError
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.action_policy_views import UnknownPolicySetError
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.egress_views import (
    BindingNotFoundError,
    BindingView,
    FluxOwnedBindingError,
    PolicyView,
    UnknownPolicyError,
)
from agentplane.sandbox_service.kubernetes_grants import (
    DuplicateKubernetesGrantError,
    KubernetesGrant,
    UnknownKubernetesGrantError,
    resolve_grants,
)
from agentplane.sandbox_service.models import SandboxNotFoundError, SandboxRunningError
from agentplane.subjects import ServiceAccountRef

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
logger = logging.getLogger(__name__)


def _models(request: Request) -> ModelCatalog:
    models = request.app.state.models
    if not isinstance(models, ModelCatalog):
        raise TypeError(f"app.state.models is {type(models).__name__}, not a ModelCatalog")
    return models


models = APIRouter(prefix="/models", tags=["models"])


@models.get("")
async def list_models(catalog: Annotated[ModelCatalog, Depends(_models)]) -> ModelCatalog:
    return catalog


preset_router = APIRouter(prefix="/presets", tags=["presets"])


def _presets(request: Request) -> PresetCatalog:
    presets = request.app.state.presets
    if not isinstance(presets, PresetCatalog):
        raise TypeError(f"app.state.presets is {type(presets).__name__}, not PresetCatalog")
    return presets


Presets = Annotated[PresetCatalog, Depends(_presets)]


@preset_router.get("")
async def list_presets(presets: Presets) -> list[SandboxPresetView]:
    return presets.views()


kubernetes_grants_router = APIRouter(prefix="/kubernetes-grants", tags=["kubernetes-grants"])


@kubernetes_grants_router.get("")
async def list_kubernetes_grants(request: Request) -> list[KubernetesGrantView]:
    return grant_views(request.app.state.kubernetes_grants)


def _inventory(request: Request) -> SandboxServiceClient:
    inventory = request.app.state.inventory
    if not isinstance(inventory, SandboxServiceClient):
        raise TypeError(f"app.state.inventory is {type(inventory).__name__}, not SandboxServiceClient")
    return inventory


Inventory = Annotated[SandboxServiceClient, Depends(_inventory)]


def _egress(request: Request) -> EgressAccess:
    egress = request.app.state.egress
    if not isinstance(egress, EgressAccess):
        raise TypeError(f"app.state.egress is {type(egress).__name__}, not EgressAccess")
    return egress


Egress = Annotated[EgressAccess, Depends(_egress)]


def _action_policy(request: Request) -> ActionPolicyInventory:
    action_policy = request.app.state.action_policy
    if not isinstance(action_policy, ActionPolicyInventory):
        raise TypeError(f"app.state.action_policy is {type(action_policy).__name__}, not ActionPolicyInventory")
    return action_policy


ActionPolicy = Annotated[ActionPolicyInventory, Depends(_action_policy)]


def _decisions(request: Request) -> DecisionsClient:
    decisions = request.app.state.decisions
    if not isinstance(decisions, DecisionsClient):
        raise TypeError(f"app.state.decisions is {type(decisions).__name__}, not DecisionsClient")
    return decisions


Decisions = Annotated[DecisionsClient, Depends(_decisions)]


@router.get("")
async def list_sandboxes(inventory: Inventory) -> list[SandboxView]:
    return [sandbox_view(view) for view in await inventory.list_sandboxes()]


@router.get("/templates")
async def list_templates(inventory: Inventory) -> list[str]:
    """The templates the operator may select in a concrete new-Sandbox request."""
    return await inventory.list_templates()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(
    request: Request, spec: NewSandbox, inventory: Inventory, caller: Annotated[CallerIdentity, Depends(require_caller)]
) -> SandboxView:
    """Create exactly the fields the caller selected; browser presets have already filled them."""
    grants = resolve_grants(spec.kubernetes_grants, request.app.state.kubernetes_grants)
    if grants and caller.kind is not CallerKind.OPERATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Kubernetes grant selection requires an operator session")
    return sandbox_view(await inventory.create(create_request(spec)))


@router.get("/{name}")
async def get_sandbox(inventory: Inventory, name: str) -> SandboxView:
    return sandbox_view(await inventory.get(name))


@router.get("/{name}/notifications")
async def sandbox_notifications(inventory: Inventory, request: Request, name: str) -> SandboxNotificationStatus:
    """Operator-authenticated BFF; never return provider payloads or workload inbox authority."""
    if request.app.state.oidc is None or request_session(request).login is None:
        raise HTTPException(403, "operator login required")
    client = request.app.state.notifications_http
    token_file = request.app.state.notifications_token_file
    if client is None or token_file is None:
        raise HTTPException(503, "notification diagnostics unavailable")
    sandbox = sandbox_view(await inventory.get(name))
    try:
        response = await client.get(
            f"/operator/v1/sandboxes/{sandbox.namespace}/{sandbox.name}/notifications",
            params={"uid": sandbox.uid},
            headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
        )
        response.raise_for_status()
    except (httpx.HTTPError, OSError) as error:
        logger.warning("notification diagnostics unavailable: %s", type(error).__name__)
        raise HTTPException(503, "notification diagnostics unavailable") from error
    return SandboxNotificationStatus.model_validate(response.json())


@router.get("/{name}/notifications/stream")
async def sandbox_notifications_stream(
    inventory: Inventory, request: Request, name: str, shutdown: Shutdown, updates: Updates
) -> StreamingResponse:
    """Operator-only SSE proxy; scope the upstream by the current Sandbox incarnation UID."""
    if request.app.state.oidc is None or request_session(request).login is None:
        raise HTTPException(403, "operator login required")
    client = request.app.state.notifications_http
    token_file = request.app.state.notifications_token_file
    if client is None or token_file is None:
        raise HTTPException(503, "notification diagnostics unavailable")
    sandbox = sandbox_view(await inventory.get(name))
    sessions = _operator_sessions(request)
    session_id = operator_session_row(request).id
    upstream = f"/operator/v1/sandboxes/{sandbox.namespace}/{sandbox.name}/notifications/stream"

    async def session_over() -> None:
        await sessions.until_ended(session_id, updates.changes[Channel.OPERATOR_SESSIONS])

    async def body() -> AsyncIterator[bytes]:
        try:
            async with client.stream(
                "GET",
                upstream,
                params={"uid": sandbox.uid},
                headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
                timeout=httpx.Timeout(None, connect=5),
            ) as response:
                response.raise_for_status()
                async for chunk in shutdown.until(until_done(response.aiter_bytes(), session_over)):
                    if await request.is_disconnected():
                        return
                    yield chunk
        except httpx.HTTPError, OSError:
            logger.warning("Notification stream interrupted", exc_info=True)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/{name}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.suspend(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.resume(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sandbox(inventory: Inventory, name: str) -> Response:
    """Delete the sandbox and everything on its volume; 409 while it is still running."""
    await inventory.delete(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class EgressGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policies: list[str] = Field(
        min_length=1, description="EgressPolicy names to grant, as one new binding naming this sandbox."
    )


@router.get("/{name}/egress")
async def sandbox_egress(inventory: Inventory, egress: Egress, name: str) -> list[BindingView]:
    """What may leave the sandbox: the bindings naming the ServiceAccount it runs as, with their
    egress policies as they resolve."""
    return await egress.bindings_for(sandbox_view(await inventory.get(name)).service_account)


@router.post("/{name}/egress", status_code=status.HTTP_201_CREATED)
async def grant_sandbox_egress(inventory: Inventory, egress: Egress, name: str, body: EgressGrant) -> BindingView:
    """Grant egress policies to a sandbox already running: a new binding naming it, never an edit of
    one it has, so this grant's expiry and revocation are its own."""
    return await egress.grant(await inventory.get(name), body.policies)


@router.get("/{name}/egress/decisions")
async def sandbox_egress_decisions(inventory: Inventory, decisions: Decisions, name: str) -> list[Decision]:
    """What recently left or was refused, from the proxy; 502 when the proxy cannot be asked."""
    return await decisions.recent(sandbox_view(await inventory.get(name)).service_account)


egress_router = APIRouter(prefix="/egress", tags=["egress"])


@egress_router.get("/policies")
async def list_policies(egress: Egress) -> list[PolicyView]:
    """The namespace's egress policies: what the create form offers to pick from."""
    return await egress.list_policies()


@egress_router.delete("/bindings/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_binding(egress: Egress, name: str) -> Response:
    """Revoke a runtime binding by deleting the rule; one from git is refused with 409."""
    await egress.revoke(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


action_policy_router = APIRouter(prefix="/action-policy", tags=["action-policy"])


@action_policy_router.get("/sets")
async def list_policy_sets(action_policy: ActionPolicy) -> list[ActionPolicySetView]:
    """The namespace's sets with the Action Service's verdict on each: what a launch picks from."""
    return await action_policy.list_policy_sets()


threads = APIRouter(prefix="/threads", tags=["threads"])


def _store(request: Request) -> ThreadStore:
    store = request.app.state.store
    if not isinstance(store, ThreadStore):
        raise TypeError(f"app.state.store is {type(store).__name__}, not ThreadStore")
    return store


def _event_logs(request: Request) -> EventLogStore:
    event_logs = request.app.state.event_logs
    if not isinstance(event_logs, EventLogStore):
        raise TypeError(f"app.state.event_logs is {type(event_logs).__name__}, not EventLogStore")
    return event_logs


def _content(request: Request) -> ContentStore:
    content = request.app.state.content
    if not isinstance(content, ContentStore):
        raise TypeError(f"app.state.content is {type(content).__name__}, not ContentStore")
    return content


Store = Annotated[ThreadStore, Depends(_store)]
EventLogs = Annotated[EventLogStore, Depends(_event_logs)]
Content = Annotated[ContentStore, Depends(_content)]


actions_router = APIRouter(prefix="/actions", tags=["actions"])
push_router = APIRouter(prefix="/push", tags=["push"])
consent_router = APIRouter(prefix="/connection-enrollments", tags=["connections"])


async def _operator_actions(
    request: Request, caller: Annotated[CallerIdentity, Depends(require_caller)]
) -> AsyncIterator[OperatorActionServiceClient]:
    try:
        yield operator_actions(request, caller)
    except OperatorFederationError as error:
        raise HTTPException(error.status_code, {"code": str(error)}) from None
    except httpx.HTTPStatusError as error:
        raise upstream_http_error(error) from error
    except httpx.RequestError as error:
        raise upstream_http_error(error) from error
    except httpx2.HTTPStatusError as error:
        raise upstream_http_error(error) from error
    except httpx2.TransportError as error:
        raise upstream_http_error(error) from error


def upstream_http_error(
    error: httpx.HTTPStatusError | httpx.RequestError | httpx2.HTTPStatusError | httpx2.TransportError,
) -> HTTPException:
    """Describe the failed request, which may be to the identity provider or the service."""
    detail = upstream_failure_detail(error)
    return HTTPException(
        detail.upstream_status if detail.upstream_status is not None else status.HTTP_503_SERVICE_UNAVAILABLE,
        detail.model_dump(),
    )


OperatorActions = Annotated[OperatorActionServiceClient, Depends(_operator_actions)]


@consent_router.post("/{handle}/preview")
async def connection_preview(request: Request, handle: EnrollmentHandle, client: OperatorActions) -> ConsentPreview:
    return await preview_enrollment(operator_session_row(request), handle, client)


@consent_router.post("/{handle}/decision")
async def connection_decision(
    request: Request, handle: EnrollmentHandle, body: ConsentDecision, client: OperatorActions
) -> EnrollmentDecisionResult:
    return await decide_enrollment(operator_session_row(request), handle, body, client)


connections_router = APIRouter(tags=["connections"])


@connections_router.get("/mcp-linkage/callback")
async def complete_mcp_linkage(state: str, code: str, client: OperatorActions) -> RedirectResponse:
    await client.complete_mcp_linkage(state, code)
    return RedirectResponse("/#/mcp-servers?linked=1", status_code=status.HTTP_303_SEE_OTHER)


@connections_router.get("/mcp-servers/{server_id}/linkage")
async def mcp_linkage(server_id: str, client: OperatorActions) -> McpLinkageView:
    return await client.mcp_linkage(server_id)


@connections_router.get("/mcp-servers")
async def list_mcp_linkages(client: OperatorActions) -> list[McpLinkageView]:
    return await client.mcp_linkages()


@connections_router.get("/action-groups")
async def action_groups(client: OperatorActions) -> list[ActionGroupView]:
    return await client.action_groups()


@connections_router.get("/mcp-servers/stream")
async def mcp_linkages_stream(
    request: Request, client: OperatorActions, shutdown: Shutdown, updates: Updates, sessions: OperatorSessions
) -> StreamingResponse:
    return _operator_resource_stream(request, shutdown, updates, sessions, client.stream_mcp_linkages, "MCP linkage")


@connections_router.get("/action-groups/stream")
async def group_health_stream(
    request: Request, client: OperatorActions, shutdown: Shutdown, updates: Updates, sessions: OperatorSessions
) -> StreamingResponse:
    return _operator_resource_stream(request, shutdown, updates, sessions, client.stream_group_health, "MCP health")


@connections_router.post("/mcp-servers/{server_id}/linkage/start")
async def start_mcp_linkage(server_id: str, body: McpLinkageStart, client: OperatorActions) -> McpLinkageStartView:
    return await client.start_mcp_linkage(server_id, body.scopes)


@connections_router.post("/mcp-servers/{server_id}/linkage/disconnect")
async def disconnect_mcp_linkage(server_id: str, client: OperatorActions) -> McpLinkageView:
    return await client.disconnect_mcp_linkage(server_id)


@connections_router.get("/connection-service-accounts")
async def connection_service_accounts(client: OperatorActions) -> list[ServiceAccountRef]:
    return await client.caller_service_accounts()


class CallerGrantView(BaseModel):
    """Account-scoped CR projections, not a new grant authority or Kubernetes RBAC evaluator."""

    model_config = ConfigDict(extra="forbid")

    egress_bindings: list[BindingView]
    action_policy: ActionPolicyView | ActionPolicyUnavailable


@connections_router.get("/caller-grants/{namespace}/{name}")
async def caller_grants(
    namespace: str,
    name: str,
    request: Request,
    caller: Annotated[CallerIdentity, Depends(require_caller)],
    egress: Egress,
    action_policy: ActionPolicy,
) -> CallerGrantView:
    """Inspect an account even without a Sandbox or current Action-caller label."""
    if caller.kind is not CallerKind.OPERATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator session required")
    subject = ServiceAccountRef(namespace=namespace, name=name)
    bindings, policy = await asyncio.gather(
        egress.bindings_for(subject), action_policy_frame(request, caller, action_policy, subject)
    )
    return CallerGrantView(egress_bindings=bindings, action_policy=policy)


@connections_router.get("/connections")
async def list_connections(client: OperatorActions) -> list[Connection]:
    return await client.connections()


@connections_router.get("/connections/stream")
async def connections_stream(
    request: Request, client: OperatorActions, shutdown: Shutdown, updates: Updates, sessions: OperatorSessions
) -> StreamingResponse:
    return _operator_resource_stream(request, shutdown, updates, sessions, client.stream_connections, "Connections")


@connections_router.get("/connections/{connection_id}")
async def get_connection(connection_id: UUID, client: OperatorActions) -> Connection:
    return await client.connection(connection_id)


@connections_router.patch("/connections/{connection_id}")
async def rename_connection(connection_id: UUID, body: ConnectionRename, client: OperatorActions) -> Connection:
    return await client.rename_connection(connection_id, body)


@connections_router.post("/connections/{connection_id}/rebind")
async def rebind_connection(connection_id: UUID, body: ConnectionRebind, client: OperatorActions) -> Connection:
    return await client.rebind_connection(connection_id, body)


@connections_router.post("/connections/{connection_id}/unbind")
async def unbind_connection(connection_id: UUID, body: ConnectionVersion, client: OperatorActions) -> Connection:
    return await client.unbind_connection(connection_id, body)


@actions_router.get("/history")
async def action_history(
    client: OperatorActions, limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = None
) -> ActionHistoryPage:
    return await client.history(limit=limit, cursor=cursor)


@actions_router.get("")
async def list_actions(
    client: OperatorActions,
    state: Annotated[list[ActionState] | None, Query(description="Only requests in these states.")] = None,
) -> list[ActionRequestView]:
    return await client.list_requests(states=tuple(state or ()))


def _operator_sessions(request: Request) -> OperatorSessionStore:
    sessions = request.app.state.operator_sessions
    if not isinstance(sessions, OperatorSessionStore):
        raise TypeError(f"app.state.operator_sessions is {type(sessions).__name__}, not OperatorSessionStore")
    return sessions


OperatorSessions = Annotated[OperatorSessionStore, Depends(_operator_sessions)]


async def _action_chunks(
    client: OperatorActions, state: Annotated[ActionState | None, Query()] = None
) -> AsyncIterator[AsyncIterator[bytes]]:
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(30):
                upstream = client.stream_requests(state=state) if state else client.stream_requests()
                chunks = await stack.enter_async_context(upstream)
        except TimeoutError as error:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Action stream startup timed out") from error
        renewed = _renewed(client, chunks, state=state)
        # A filtered snapshot is small and also the resync signal on reconnect. Do not suppress
        # it when it happens to be identical: history may have changed while we were offline.
        yield renewed if state else _without_repeated_snapshots(renewed)


async def _renewed(
    client: OperatorActionServiceClient, opened: AsyncIterator[bytes], *, state: ActionState | None = None
) -> AsyncIterator[bytes]:
    """`opened`, then the upstream opened again under a freshly exchanged token each time one ends, as
    one does when the minute-long token it was opened with expires. One that ends before its first
    chunk refused the token at the door; it is not opened again, so a refusal cannot loop."""
    upstream: AbstractAsyncContextManager[AsyncIterator[bytes]] = nullcontext(opened)
    while True:
        delivered = False
        async with upstream as chunks:
            async for chunk in chunks:
                delivered = True
                yield chunk
        if not delivered:
            return
        upstream = client.stream_requests(state=state) if state else client.stream_requests()


async def _without_repeated_snapshots(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """`chunks` regrouped into whole SSE frames, less each `snapshot` identical to the last one
    forwarded. A snapshot is the whole Action history, megabytes (#7922), and every upstream
    `_renewed` opens starts with one, however little has changed."""
    pending = bytearray()
    last_snapshot: bytes | None = None
    async for chunk in chunks:
        # A boundary split between two chunks starts at the last byte already held.
        searched = max(len(pending) - 1, 0)
        pending += chunk
        while (end := pending.find(b"\n\n", searched)) != -1:
            frame = bytes(pending[: end + 2])
            del pending[: end + 2]
            searched = 0
            if frame.startswith(b"event: snapshot\n"):
                if (digest := hashlib.sha256(frame).digest()) == last_snapshot:
                    continue
                last_snapshot = digest
            yield frame


@actions_router.get("/stream")
async def action_stream(
    request: Request,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessions,
    chunks: Annotated[AsyncIterator[bytes], Depends(_action_chunks)],
) -> StreamingResponse:
    session_id = operator_session_row(request).id

    async def session_over() -> None:
        # The replicas share its end -- a logout on any of them, or its expiry -- through PostgreSQL.
        await sessions.until_ended(session_id, updates.changes[Channel.OPERATOR_SESSIONS])

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in shutdown.until(until_done(chunks, session_over)):
                if await request.is_disconnected():
                    return
                yield chunk
        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            httpx2.HTTPStatusError,
            httpx2.TransportError,
            OperatorFederationError,
        ):
            # Headers are already sent. End the SSE connection so EventSource reconnects.
            logger.warning("Action stream interrupted after response start", exc_info=True)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@actions_router.get("/{request_id}")
async def get_action(request_id: UUID, client: OperatorActions) -> ActionRequestView:
    return await client.get(request_id)


@actions_router.get("/{request_id}/events")
async def action_events(
    request_id: UUID,
    client: OperatorActions,
    after_sequence: Annotated[int, Query(ge=0, description="Events with a greater sequence.")] = 0,
) -> list[ActionEventView]:
    return await client.events(request_id, after_sequence=after_sequence)


@actions_router.post("/{request_id}/decision")
async def decide_action(request_id: UUID, body: DecisionInput, client: OperatorActions) -> ActionRequestView:
    return await client.decide(request_id, body)


@push_router.get("/config")
async def push_config(client: OperatorActions) -> dict[str, str | None]:
    return await client.push_config()


@push_router.get("/subscriptions")
async def push_subscriptions(client: OperatorActions) -> list[dict[str, object]]:
    return await client.push_subscriptions()


def _operator_resource_stream(
    request: Request,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessions,
    source: Callable[[], AbstractAsyncContextManager[AsyncIterator[bytes]]],
    label: str,
) -> StreamingResponse:
    """Proxy an operator SSE resource, renewing upstream tokens and ending on logout."""
    session_id = operator_session_row(request).id

    async def session_over() -> None:
        await sessions.until_ended(session_id, updates.changes[Channel.OPERATOR_SESSIONS])

    async def upstream() -> AsyncIterator[bytes]:
        while True:
            delivered = False
            async with source() as chunks:
                async for chunk in chunks:
                    delivered = True
                    yield chunk
            if not delivered:
                return

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in shutdown.until(until_done(upstream(), session_over)):
                if await request.is_disconnected():
                    return
                yield chunk
        except httpx.HTTPError, httpx2.TransportError, OperatorFederationError:
            logger.warning("%s stream interrupted", label, exc_info=True)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@push_router.get("/subscriptions/stream")
async def push_subscriptions_stream(
    request: Request, client: OperatorActions, shutdown: Shutdown, updates: Updates, sessions: OperatorSessions
) -> StreamingResponse:
    return _operator_resource_stream(
        request, shutdown, updates, sessions, client.stream_push_subscriptions, "Push settings"
    )


@push_router.post("/subscriptions", status_code=204)
async def register_push_subscription(body: dict[str, str], client: OperatorActions, request: Request) -> None:
    await client.register_push(body, user_agent=request.headers.get("user-agent", "")[:300])


@push_router.delete("/subscriptions", status_code=204)
async def remove_push_subscription(endpoint: str, client: OperatorActions) -> None:
    await client.remove_push(endpoint)


@push_router.post("/decision/{request_id}")
async def push_decision(request_id: UUID, body: DecisionInput, client: OperatorActions) -> ActionRequestView:
    return await client.decide(request_id, body)


class ThreadRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        max_length=200, description="The new name, whitespace-trimmed; blank or null leaves the thread unnamed."
    )

    @field_validator("name", mode="before")
    @classmethod
    def _blank_is_unnamed(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class CommandReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_epoch: str
    command_ids: list[str] = Field(max_length=128)


class CommandReconciliationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    outcome: CommandOutcome | None


class CommandReconciliationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_epoch: str
    commands: list[CommandReconciliationEntry]


@threads.get("")
async def list_threads(
    store: Store,
    sandbox: Annotated[str | None, Query(description="Only threads of this sandbox.")] = None,
    session_id: Annotated[
        str | None, Query(description="Only threads of this public Sandbox Service Session UUID.")
    ] = None,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
) -> list[ThreadView]:
    """Every persisted thread, newest first; a thread outlives its sandbox. Both filters together
    name at most one thread: a session's."""
    return await store.list_threads(sandbox=sandbox, session_id=session_id, include_archived=include_archived)


class ThreadsWithSandboxes(BaseModel):
    """All visible Threads and every existing Sandbox, including those without Threads. A Thread's
    `sandbox` name absent from `sandboxes` means that Sandbox row is gone."""

    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadView]
    sandboxes: dict[str, SandboxView]


@threads.get("/with-sandboxes")
async def list_threads_with_sandboxes(
    store: Store,
    inventory: Inventory,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
) -> ThreadsWithSandboxes:
    """Visible Threads newest first, joined with the complete Sandbox inventory."""
    thread_views = await store.list_threads(include_archived=include_archived)
    sandboxes = {view.name: sandbox_view(view) for view in await inventory.list_sandboxes()}
    return ThreadsWithSandboxes(threads=thread_views, sandboxes=sandboxes)


@threads.get("/{thread_id}")
async def get_thread(store: Store, thread_id: UUID) -> ThreadView:
    view = await store.get_thread(thread_id)
    if view is None:
        raise ThreadNotFoundError(thread_id)
    return view


@threads.post("/{thread_id}/resume")
async def resume_thread(store: Store, bridge: runner_bridge.Bridge, thread_id: UUID) -> dict[str, object]:
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    if thread.archived:
        raise HTTPException(status.HTTP_409_CONFLICT, "an archived Thread cannot be resumed")
    return MessageToDict(
        await bridge.resume_thread(thread_id, expected_harness=thread.harness.value, expected_cwd=thread.cwd)
    )


@threads.post("/{thread_id}/commands/reconcile")
async def reconcile_commands(
    content: Content, thread_id: UUID, body: CommandReconciliationRequest
) -> CommandReconciliationResponse:
    try:
        outcomes = await content.command_outcomes(thread_id, body.projection_epoch, body.command_ids)
    except ThreadScopeResetError as error:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(error)) from error
    return CommandReconciliationResponse(
        projection_epoch=body.projection_epoch,
        commands=[
            CommandReconciliationEntry(command_id=command_id, outcome=outcome)
            for command_id, outcome in outcomes.items()
        ],
    )


@threads.patch("/{thread_id}")
async def rename_thread(store: Store, thread_id: UUID, body: ThreadRename) -> ThreadView:
    return await store.rename(thread_id, body.name)


@threads.post("/{thread_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_thread(store: Store, bridge: runner_bridge.Bridge, inventory: Inventory, thread_id: UUID) -> Response:
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    try:
        sandbox = sandbox_view(await inventory.get(thread.sandbox))
    except SandboxNotFoundError:
        # A deleted Sandbox has no running harness to keep visible.
        pass
    else:
        if sandbox.pod is not None:
            if not sandbox_has_ready_pod(sandbox):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "cannot verify the runner while a Pod exists; wait for its status to become ready or for it to be removed",
                )
            sessions = await bridge.list_sessions(thread.sandbox)
            if any(
                session.session_id == thread.session_id and session.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
                for session in sessions
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "stop the harness before archiving this thread")
    await store.archive(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@threads.post("/{thread_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_thread(store: Store, thread_id: UUID) -> Response:
    await store.unarchive(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@threads.post("/{thread_id}/commands")
async def thread_command(
    request: Request,
    store: Store,
    content: Content,
    catalog: Annotated[ModelCatalog, Depends(_models)],
    thread_id: UUID,
    body: dict[str, object],
) -> dict[str, object]:
    """Return the runner's exact durable CommandAdmitted EventEntry.

    The app archive may still lag this receipt. An exact retry is served from the archive
    when present, or deduplicated by the runner; neither response promises native effect.
    """
    command = runner_bridge.parse_command(body)
    if not command.command_id or command.WhichOneof("operation") is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="command requires id and operation"
        )
    if admitted := await content.admitted_command(thread_id, command):
        return MessageToDict(admitted)
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    if command.HasField("change_model") and (
        not command.change_model.model or command.change_model.model not in catalog.harnesses[thread.harness]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="model is incompatible with this thread's harness"
        )
    bridge = request.app.state.bridge
    if not isinstance(bridge, runner_bridge.RunnerBridge):
        raise TypeError(f"app.state.bridge is {type(bridge).__name__}, not RunnerBridge")
    return MessageToDict(await bridge.command(thread_id, command))


# A fold cursor is 64-bit and a JavaScript number is not, so it travels as a decimal
# string -- the representation every fold response model already publishes it in. Declaring
# it `int` here would put `integer` in the schema and make every browser caller cast past it. The
# range check the string form loses is restored here: the column is a signed 64-bit integer, and a
# value past it must be refused as a bad request rather than reaching the driver as one.
_DECIMAL = r"^\d+$"
_INT64_MAX = 2**63 - 1


def _within_int64(value: str) -> str:
    if int(value) > _INT64_MAX:
        raise ValueError(f"cursor is outside the signed 64-bit range: {value}")
    return value


DecimalCursor = Annotated[str, Query(pattern=_DECIMAL), AfterValidator(_within_int64)]
DecimalCursorPath = Annotated[str, Path(pattern=_DECIMAL), AfterValidator(_within_int64)]


@threads.get("/{thread_id}/evidence")
async def thread_evidence(
    thread_id: UUID,
    content: Content,
    projection_epoch: str,
    entity_kind: str,
    entity_id: str,
    after_cursor: DecimalCursor = "0",
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> EvidencePage:
    return await content.evidence(
        thread_id,
        projection_epoch=projection_epoch,
        entity_kind=entity_kind,
        entity_id=entity_id,
        after_cursor=int(after_cursor),
        limit=limit,
    )


@threads.get("/{thread_id}/evidence/{observation_cursor}/frames")
async def thread_native_frames(
    thread_id: UUID,
    observation_cursor: DecimalCursorPath,
    content: Content,
    projection_epoch: str,
    entity_kind: str,
    entity_id: str,
    after_sequence: DecimalCursor = "0",
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> NativeFramePage:
    return await content.native_frames(
        thread_id,
        projection_epoch=projection_epoch,
        entity_kind=entity_kind,
        entity_id=entity_id,
        observation_cursor=int(observation_cursor),
        after_sequence=int(after_sequence),
        limit=limit,
    )


@threads.get("/{thread_id}/observations/{cursor}")
async def thread_observation_entry(thread_id: UUID, cursor: int, event_logs: EventLogs) -> ArchivedObservationEntry:
    """The raw entry behind one listed observation, read only when a reader expands it."""
    entry = await event_logs.observation_entry(thread_id, cursor)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no observation at {cursor} in this thread")
    return entry


@threads.get("/{thread_id}/observations")
async def thread_observations(
    thread_id: UUID,
    store: Store,
    event_logs: EventLogs,
    before_cursor: DecimalCursor | None = None,
    after_cursor: DecimalCursor | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> ObservationPage:
    """Original chronological observations; default to the tail, including unlinked debug data."""
    if before_cursor is not None and after_cursor is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="select either before_cursor or after_cursor"
        )
    if await store.get_thread(thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    return await event_logs.observations(
        thread_id,
        before_cursor=None if before_cursor is None else int(before_cursor),
        after_cursor=None if after_cursor is None else int(after_cursor),
        limit=limit,
    )


@threads.get("/{thread_id}/events")
async def thread_events(
    store: Store,
    event_logs: EventLogs,
    thread_id: UUID,
    after: Annotated[int, Query(ge=0, description="EventEntries with a greater cursor.")] = 0,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 10_000,
) -> list[dict[str, object]]:
    """The stored EventEntries as proto-JSON, in cursor order."""
    if await store.get_thread(thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    return [MessageToDict(entry) for entry in await event_logs.events(thread_id, after_cursor=after, limit=limit)]


@threads.get("/{thread_id}/events/stream")
async def thread_event_stream(
    store: Store,
    event_logs: EventLogs,
    updates: Updates,
    shutdown: Shutdown,
    thread_id: UUID,
    after: Annotated[int, Query(ge=0, description="Replay EventEntries with a greater cursor.")] = 0,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    # EventSource reconnect carries its verified wire position, overriding a stale query.
    cursor = last_event_id if last_event_id is not None else after
    if cursor > await event_logs.read_watermark(thread_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "cursor is beyond the archived Thread prefix")
    return StreamingResponse(
        shutdown.until(stream.follow(event_logs, updates.changes[Channel.THREADS], thread_id, after_cursor=cursor)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app(
    inventory: SandboxServiceClient,
    bridge: runner_bridge.RunnerBridge,
    store: ThreadStore,
    catalog: ModelCatalog,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live: LiveIndex,
    action_policy: ActionPolicyInventory,
    oidc: OIDCSettings | None = None,
    reviewer: TokenReviewer | None = None,
    presets: PresetCatalog | None = None,
    operator_actions: FederatedOperatorActions | None = None,
    electric: ElectricProxy | None = None,
    kubernetes_grants: dict[str, KubernetesGrant] | None = None,
    *,
    event_logs: EventLogStore,
    content: ContentStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    notifications_http: httpx.AsyncClient | None = None,
    notifications_token_file: FilePath | None = None,
) -> FastAPI:
    """The whole HTTP surface, guarded. Each of `oidc` and `reviewer` enables one way to authenticate,
    and an app given neither answers 401 to everything but /healthz."""
    if set(catalog.harnesses) != set(Harness) or not any(catalog.harnesses.values()):
        raise ValueError(f"the model catalog needs every harness key and at least one offered model: {catalog=}")
    configured_presets = presets or PresetCatalog()
    configured_grants = kubernetes_grants or {}
    grant_views(configured_grants)  # validate catalog keys before serving requests
    for name, preset in configured_presets.sandboxes.items():
        try:
            resolve_grants(preset.kubernetes_grants, configured_grants)
        except (UnknownKubernetesGrantError, DuplicateKubernetesGrantError) as error:
            raise ValueError(f"SandboxPreset {name!r}: {error}") from error
    for thread_preset_name, thread_preset in configured_presets.threads.items():
        if thread_preset.model not in catalog.harnesses[thread_preset.harness]:
            raise ValueError(
                f"ThreadPreset {thread_preset_name!r} names model {thread_preset.model!r} outside the configured catalog"
            )
        option = next(option for option in catalog.models if option.model == thread_preset.model)
        if (
            thread_preset.reasoning_effort is not None
            and thread_preset.reasoning_effort not in option.reasoning_efforts
        ):
            raise ValueError(
                f"ThreadPreset {thread_preset_name!r} reasoning effort {thread_preset.reasoning_effort!r} "
                f"is not supported by {thread_preset.model!r}"
            )
    app = FastAPI(title="Agentplane", version="0")

    @app.exception_handler(ServiceError)
    async def sandbox_service_error(request: Request, error: ServiceError) -> JSONResponse:
        code = {
            grpc.StatusCode.NOT_FOUND: 404,
            grpc.StatusCode.FAILED_PRECONDITION: 409,
            grpc.StatusCode.ALREADY_EXISTS: 409,
            grpc.StatusCode.INVALID_ARGUMENT: 422,
        }.get(error.code, 503)
        return JSONResponse({"detail": str(error)}, status_code=code)

    @app.exception_handler(HistoryServiceError)
    async def history_service_error(request: Request, error: HistoryServiceError) -> JSONResponse:
        code = {grpc.StatusCode.NOT_FOUND: 404, grpc.StatusCode.INVALID_ARGUMENT: 422}.get(error.code, 503)
        return JSONResponse({"detail": str(error)}, status_code=code)

    @app.exception_handler(ConnectionError)
    async def sandbox_service_unavailable(request: Request, error: ConnectionError) -> JSONResponse:
        return JSONResponse({"detail": "Sandbox Service unavailable; outcome may be uncertain"}, status_code=503)

    @app.exception_handler(TimeoutError)
    async def sandbox_service_deadline(request: Request, error: TimeoutError) -> JSONResponse:
        return JSONResponse({"detail": "Upstream deadline expired; mutation outcome may be uncertain"}, status_code=504)

    app.state.inventory = inventory
    app.state.bridge = bridge
    app.state.store = store
    app.state.event_logs = event_logs
    app.state.content = content
    app.state.database_updates = database_updates
    app.state.operator_sessions = operator_sessions
    app.state.notifications_http = notifications_http
    app.state.notifications_token_file = notifications_token_file
    app.state.models = catalog
    app.state.presets = configured_presets
    app.state.kubernetes_grants = configured_grants
    app.state.egress = egress
    app.state.action_policy = action_policy
    app.state.decisions = decisions
    app.state.live = live
    app.state.oidc = oidc
    app.state.reviewer = reviewer
    app.state.operator_actions = operator_actions
    app.state.electric = electric
    app.state.drain = Drain()
    # Every route needs a caller. There is no unauthenticated path into the API: /healthz is
    # declared below, outside these routers.
    for api_router in (
        router,
        models,
        preset_router,
        kubernetes_grants_router,
        runner_bridge.router,
        threads,
        actions_router,
        push_router,
        consent_router,
        connections_router,
        egress_router,
        action_policy_router,
        live_router,
    ):
        app.include_router(api_router, dependencies=[Depends(require_caller)])
    if electric is not None:
        app.include_router(electric_router, dependencies=[Depends(require_caller)])
    if oidc is not None:
        app.add_middleware(
            OperatorSessionMiddleware,
            store=operator_sessions,
            secret_key=oidc.session_secret,
            session_cookie=oidc.cookie_name,
            https_only=oidc.secure,
            max_age=oidc.session_max_seconds,
            idle_seconds=oidc.session_idle_seconds,
            activity_step_seconds=oidc.session_activity_step_seconds,
        )
        app.state.oauth = build_oauth(oidc)
        # Unguarded, because these are how a browser with no credential acquires one.
        app.include_router(auth_routes.router)
    # Outermost, so a request the drain refuses touches nothing below it.
    app.add_middleware(DrainMiddleware, drain=app.state.drain, liveness_path="/healthz")

    @app.exception_handler(ThreadScopeChangedError)
    async def _thread_scope_changed(_request: Request, error: ThreadScopeChangedError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=status.HTTP_410_GONE)

    @app.exception_handler(ThreadEvidenceNotFoundError)
    async def _evidence_missing(_request: Request, error: ThreadEvidenceNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(ThreadNotFoundError)
    async def _thread_not_found(_request: Request, error: ThreadNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(CommandIdConflictError)
    async def _command_id_conflict(_request: Request, error: CommandIdConflictError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        # The Deployment's liveness probe: the process serves; the inventory's own reachability is
        # per request. Answered through the drain, unlike everything else.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        # The Deployment's readiness probe: the drain middleware answers 503 here once shutdown begins.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.exception_handler(SandboxNotFoundError)
    async def _not_found(_request: Request, error: SandboxNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(SandboxRunningError)
    async def _still_running(_request: Request, error: SandboxRunningError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(BindingNotFoundError)
    async def _binding_not_found(_request: Request, error: BindingNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(DecisionsUnavailableError)
    async def _decisions_unavailable(_request: Request, error: DecisionsUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(error)})

    @app.exception_handler(FluxOwnedBindingError)
    async def _flux_owned(_request: Request, error: FluxOwnedBindingError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(UnknownPolicyError)
    async def _unknown_policy(_request: Request, error: UnknownPolicyError) -> JSONResponse:
        # 422 rather than 404: the sandbox in the path is there, and 404 on these routes already
        # says it is not. The body parsed and named something that does not resolve.
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(UnknownPolicySetError)
    async def _unknown_policy_set(_request: Request, error: UnknownPolicySetError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(UnknownKubernetesGrantError)
    async def _unknown_kubernetes_grant(_request: Request, error: UnknownKubernetesGrantError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(DuplicateKubernetesGrantError)
    async def _duplicate_kubernetes_grant(_request: Request, error: DuplicateKubernetesGrantError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(SandboxNotReachableError)
    async def _not_reachable(_request: Request, error: SandboxNotReachableError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.RunnerAdmissionTimeoutError)
    @app.exception_handler(OpenTimeoutError)
    async def _runner_timed_out(
        _request: Request, error: runner_bridge.RunnerAdmissionTimeoutError | OpenTimeoutError
    ) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_504_GATEWAY_TIMEOUT, content={"detail": str(error)})

    @app.exception_handler(RunnerError)
    async def _runner_refused(_request: Request, error: RunnerError) -> JSONResponse:
        # The runner refused an Open or a command: an unknown session, a spec mismatch, a bad cursor.
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.MalformedMessageError)
    async def _malformed(_request: Request, error: runner_bridge.MalformedMessageError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    return app
