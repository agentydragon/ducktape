"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, nullcontext
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Annotated, Any
from uuid import UUID

import grpc
import httpx
import httpx2
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from agentplane.action_service.catalog import ActionGroupView
from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.connections import Connection, ConnectionRebind, ConnectionRename, ConnectionVersion
from agentplane.action_service.enrollments import EnrollmentDecisionResult
from agentplane.action_service.mcp_linkage import McpLinkageStart, McpLinkageStartView, McpLinkageView
from agentplane.action_service.models import ActionHistoryPage, ActionRequestView, ActionState, DecisionInput
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
from agentplane.app.database_updates import Channel
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy, router as electric_router
from agentplane.app.identity import CallerKind, TokenReviewer, require_caller
from agentplane.app.live import (
    ActionPolicy,
    Caller,
    LiveIndex,
    Store,
    Updates,
    action_policy_frame,
    router as live_router,
)
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.oidc import OIDCSettings, build_oauth
from agentplane.app.operator_sessions import (
    OperatorSessionMiddleware,
    OperatorSessionStore,
    operator_session_row,
    request_session,
)
from agentplane.app.presets import PresetCatalog, SandboxPresetView
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.sandbox_models import NewSandbox, SandboxView, create_request, sandbox_is_ready, sandbox_view
from agentplane.app.shutdown import Drain, DrainMiddleware, Shutdown, until_done
from agentplane.app.system_status import ComponentStatus, SystemStatusChecker
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
from agentplane.app.threads.runner_directory import SandboxNotReachableError
from agentplane.app.threads.store import MAX_PAGE, InvalidPageTokenError, ThreadPage, ThreadStore
from agentplane.app.threads.thread_view import ThreadView
from agentplane.app.threads.view.content import CommandIdConflictError, ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import RefusedError, ServiceError
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress_views import (
    BindingNotFoundError,
    BindingView,
    FluxOwnedBindingError,
    SandboxOwnedBindingError,
)
from agentplane.sandboxes.errors import (
    ConsoleUnavailableError,
    InvalidSandboxError,
    SandboxConflictError,
    SandboxNotFoundError,
    SandboxRunningError,
    SandboxTearingDownError,
    UnknownPolicySetError,
)
from agentplane.sse import frame, sse_response
from agentplane.subjects import ServiceAccountRef

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
logger = logging.getLogger(__name__)


@dataclass
class AppServices:
    """Everything the app's routes use, built once by `create_app` and held as `app.state.services`.

    The modules below this one (`live`, `electric`, `identity`, `oidc`, `shutdown`, `action_federation`,
    `auth_routes`, `threads.bridge`) read their own field by name: they define the types this class
    names, so importing it would be a dependency cycle.
    """

    inventory: SandboxServiceClient | SandboxAccess
    bridge: runner_bridge.RunnerBridge
    store: ThreadStore
    event_logs: EventLogStore
    content: ContentStore
    database_updates: WakeupListener
    operator_sessions: OperatorSessionStore
    notifications_http: httpx.AsyncClient
    notifications_token_file: FilePath
    system_status: SystemStatusChecker
    models: ModelCatalog
    presets: PresetCatalog
    egress: EgressAccess
    action_policy: ActionPolicyInventory
    decisions: DecisionsClient
    live: LiveIndex
    oidc: OIDCSettings | None
    reviewer: TokenReviewer | None
    operator_actions: FederatedOperatorActions | None
    electric: ElectricProxy
    oauth: OAuth | None = field(init=False)
    drain: Drain = field(default_factory=Drain)

    def __post_init__(self) -> None:
        self.oauth = None if self.oidc is None else build_oauth(self.oidc)


def app_services(app: FastAPI) -> AppServices:
    services = app.state.services
    if not isinstance(services, AppServices):
        raise TypeError(f"app.state.services is {type(services).__name__}, not AppServices")
    return services


def _service[T](pick: Callable[[AppServices], T]) -> Any:
    """A `Depends` on one of the app's services, for an `Annotated` alias."""

    def dependency(request: Request) -> T:
        return pick(app_services(request.app))

    return Depends(dependency)


Services = Annotated[AppServices, _service(lambda services: services)]
Inventory = Annotated[SandboxServiceClient | SandboxAccess, _service(lambda services: services.inventory)]
Egress = Annotated[EgressAccess, _service(lambda services: services.egress)]
EventLogs = Annotated[EventLogStore, _service(lambda services: services.event_logs)]
Content = Annotated[ContentStore, _service(lambda services: services.content)]
OperatorSessions = Annotated[OperatorSessionStore, _service(lambda services: services.operator_sessions)]
Models = Annotated[ModelCatalog, _service(lambda services: services.models)]


def _require_operator_login(request: Request) -> None:
    # Workload authentication of the app is not operator authorization to inspect another inbox.
    if app_services(request.app).oidc is None or request_session(request).login is None:
        raise HTTPException(403, "operator login required")


OperatorLogin = Depends(_require_operator_login)

models = APIRouter(prefix="/models", tags=["models"])


@models.get("")
async def list_models(catalog: Models) -> ModelCatalog:
    return catalog


preset_router = APIRouter(prefix="/presets", tags=["presets"])


@preset_router.get("")
async def list_presets(services: Services) -> list[SandboxPresetView]:
    return services.presets.views()


@router.get("")
async def list_sandboxes(inventory: Inventory) -> list[SandboxView]:
    return [sandbox_view(view) for view in await inventory.list_sandboxes()]


@router.get("/templates")
async def list_templates(inventory: Inventory) -> list[str]:
    """The templates the operator may select in a concrete new-Sandbox request."""
    return await inventory.list_templates()


class SandboxForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_sets: list[str] = Field(description="The PolicySets a new Sandbox may name.")


@router.get("/form")
async def sandbox_form(services: Services) -> SandboxForm:
    """What the create form offers."""
    return SandboxForm(policy_sets=await services.action_policy.policy_set_names())


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(spec: NewSandbox, services: Services) -> SandboxView:
    """Create exactly the fields the caller selected; browser presets have already filled them."""
    if spec.preset is not None and spec.preset not in services.presets.sandboxes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"no preset named {spec.preset!r}")
    return sandbox_view(await services.inventory.create(create_request(spec)))


@router.get("/{name}")
async def get_sandbox(inventory: Inventory, name: str) -> SandboxView:
    return sandbox_view(await inventory.get(name))


@router.get("/{name}/notifications/stream", dependencies=[OperatorLogin])
async def sandbox_notifications_stream(
    request: Request, services: Services, name: str, shutdown: Shutdown, updates: Updates
) -> StreamingResponse:
    """Operator-only SSE proxy; scope the upstream by the current Sandbox incarnation UID."""
    sandbox = sandbox_view(await services.inventory.get(name))
    return _notifications_proxy(
        request,
        services,
        shutdown,
        updates,
        f"/v1/operator/sandboxes/{sandbox.namespace}/{sandbox.name}/notifications/stream",
        {"uid": str(sandbox.uid)},
    )


def _notifications_proxy(
    request: Request,
    services: AppServices,
    shutdown: Shutdown,
    updates: Updates,
    upstream: str,
    params: dict[str, str | list[str]],
) -> StreamingResponse:
    """Relay a Notification Service operator stream for as long as the operator session lasts."""

    async def chunks() -> AsyncIterator[bytes]:
        async with services.notifications_http.stream(
            "GET",
            upstream,
            params=params,
            headers={"Authorization": f"Bearer {services.notifications_token_file.read_text().strip()}"},
            timeout=httpx.Timeout(None, connect=5),
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    return _operator_relay(
        request, shutdown, updates, services.operator_sessions, chunks(), "Notification", (httpx.HTTPError, OSError)
    )


def _operator_relay(
    request: Request,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessionStore,
    chunks: AsyncIterator[bytes],
    label: str,
    errors: tuple[type[Exception], ...],
) -> StreamingResponse:
    """Relay `chunks` until the operator session ends -- a logout on any replica, or its expiry, which
    they share through PostgreSQL -- the app drains, or the browser goes. Headers are already sent
    when `errors` interrupt the upstream, so the SSE connection just ends and EventSource reconnects."""
    session_id = operator_session_row(request).id

    async def session_over() -> None:
        await sessions.until_ended(session_id, updates.changes[Channel.OPERATOR_SESSIONS])

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in shutdown.until(until_done(chunks, session_over)):
                if await request.is_disconnected():
                    return
                yield chunk
        except errors:
            logger.warning("%s stream interrupted", label, exc_info=True)

    return sse_response(body())


@router.get("/{name}/console/stream", dependencies=[OperatorLogin])
async def sandbox_console_stream(
    request: Request, services: Services, name: str, shutdown: Shutdown, updates: Updates
) -> StreamingResponse:
    """Operator-only SSE of a VM Sandbox's serial console from now on; each `output` event carries
    base64 bytes. The console is output-only here: nothing reaches the guest."""
    # Fail before the response starts, while the status can still be an HTTP error.
    sandbox = sandbox_view(await services.inventory.get(name))
    console = services.inventory.stream_console(sandbox.name)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for output in console:
                yield frame("output", base64.b64encode(output).decode())
        except ServiceError as error:
            if error.code != grpc.StatusCode.FAILED_PRECONDITION:
                raise
            # A Pod Sandbox, a stopped VM or a console someone else holds: retrying will not help.
            yield frame("unavailable", str(error))
        except ConsoleUnavailableError as error:
            yield frame("unavailable", str(error))

    return _operator_relay(
        request, shutdown, updates, services.operator_sessions, chunks(), "Console", (ServiceError, OSError)
    )


@router.post("/{name}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_sandbox(inventory: Inventory, name: str) -> None:
    await inventory.suspend(name)


@router.post("/{name}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_sandbox(inventory: Inventory, name: str) -> None:
    await inventory.resume(name)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sandbox(inventory: Inventory, name: str) -> None:
    """Start deleting the sandbox and everything on its volume, once its runner's ingress has
    acknowledged every Session's seal; 409 while it is still running."""
    await inventory.delete(name)


class PolicyGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_sets: list[str] = Field(
        min_length=1, description="PolicySet names of any rule type to grant, as one new binding naming this sandbox."
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="When the Policy Controller deletes the binding; absent grants until revoked."
    )


@router.get("/{name}/egress")
async def sandbox_egress(inventory: Inventory, egress: Egress, name: str) -> list[BindingView]:
    """What may leave the sandbox: the bindings naming the ServiceAccount it runs as, with their
    egress policies as they resolve."""
    return await egress.bindings_for(sandbox_view(await inventory.get(name)).service_account)


@router.post("/{name}/policy-bindings", status_code=status.HTTP_201_CREATED, dependencies=[OperatorLogin])
async def grant_sandbox_policy_sets(inventory: Inventory, egress: Egress, name: str, body: PolicyGrant) -> BindingView:
    """Grant policy sets of any rule type to a sandbox already running: a new binding naming it,
    never an edit of one it has, so this grant's expiry and revocation are its own. Operator login
    only: a workload token must not be able to widen anyone's authority through the app."""
    return await egress.grant(await inventory.get(name), body.policy_sets, expires_at=body.expires_at)


system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/status")
async def read_system_status(services: Services) -> list[ComponentStatus]:
    return await services.system_status.check_all()


policy_binding_router = APIRouter(prefix="/policy-bindings", tags=["policy-bindings"])


@policy_binding_router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[OperatorLogin])
async def revoke_binding(egress: Egress, name: str) -> None:
    """Revoke a runtime binding by deleting it; one from git or the Sandbox's own is refused with 409."""
    await egress.revoke(name)


action_policy_router = APIRouter(prefix="/action-policy", tags=["action-policy"])


@action_policy_router.get("/sets")
async def list_policy_sets(action_policy: ActionPolicy) -> list[ActionPolicySetView]:
    """The namespace's sets with the Action Service's verdict on each: what a launch picks from."""
    return await action_policy.list_policy_sets()


threads = APIRouter(prefix="/threads", tags=["threads"])


actions_router = APIRouter(prefix="/actions", tags=["actions"])
push_router = APIRouter(prefix="/push", tags=["push"])
consent_router = APIRouter(prefix="/connection-enrollments", tags=["connections"])


async def _operator_actions(request: Request, caller: Caller) -> AsyncIterator[OperatorActionServiceClient]:
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
    namespace: str, name: str, request: Request, caller: Caller, egress: Egress, action_policy: ActionPolicy
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
    return _operator_relay(
        request,
        shutdown,
        updates,
        sessions,
        chunks,
        "Action",
        (httpx.HTTPError, httpx2.HTTPStatusError, httpx2.TransportError, OperatorFederationError),
    )


@actions_router.get("/{request_id}")
async def get_action(request_id: UUID, client: OperatorActions) -> ActionRequestView:
    return await client.get(request_id)


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

    async def upstream() -> AsyncIterator[bytes]:
        while True:
            delivered = False
            async with source() as chunks:
                async for chunk in chunks:
                    delivered = True
                    yield chunk
            if not delivered:
                return

    return _operator_relay(
        request,
        shutdown,
        updates,
        sessions,
        upstream(),
        label,
        (httpx.HTTPError, httpx2.TransportError, OperatorFederationError),
    )


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


@threads.get("")
async def list_threads(
    store: Store,
    sandbox: Annotated[str | None, Query(description="Only threads of this sandbox.")] = None,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
    search: Annotated[
        str | None,
        Query(
            min_length=1, description="Only threads whose name or sandbox contains this, or whose ID starts with it."
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE, description="At most this many threads.")] = 50,
    page_token: Annotated[
        str | None, Query(description="A previous page's `next_page_token`, with the same filters.")
    ] = None,
) -> ThreadPage:
    """Persisted threads, newest first; a thread outlives its sandbox."""
    return await store.list_page(
        limit=limit, page_token=page_token, sandbox=sandbox, search=search, include_archived=include_archived
    )


@threads.get("/notices/stream", dependencies=[OperatorLogin])
async def thread_notices_stream(
    request: Request,
    services: Services,
    shutdown: Shutdown,
    updates: Updates,
    thread_id: Annotated[list[UUID], Query(max_length=MAX_PAGE, description="The threads to follow.")],
) -> StreamingResponse:
    """Operator-only SSE of the named threads' live notification inboxes: entries queued for a
    notice, entries a notice delivered that the agent has not acknowledged, and how far the latest
    notice got. Following it acknowledges nothing."""
    return _notifications_proxy(
        request,
        services,
        shutdown,
        updates,
        "/v1/operator/notices/stream",
        {"session_id": [str(thread) for thread in thread_id]},
    )


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
        await bridge.resume_thread(thread_id, expected_harness=thread.harness, expected_cwd=thread.cwd)
    )


@threads.patch("/{thread_id}")
async def rename_thread(store: Store, thread_id: UUID, body: ThreadRename) -> ThreadView:
    return await store.rename(thread_id, body.name)


@threads.post("/{thread_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_thread(store: Store, bridge: runner_bridge.Bridge, inventory: Inventory, thread_id: UUID) -> None:
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
            if not sandbox_is_ready(sandbox):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "cannot verify the runner while a Pod exists; wait for its status to become ready or for it to be removed",
                )
            sessions = await bridge.list_sessions(thread.sandbox)
            if any(
                session.session_id == str(thread.id) and session.harness_state == runner_pb2.HARNESS_STATE_RUNNING
                for session in sessions
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "stop the harness before archiving this thread")
    await store.archive(thread_id)


@threads.post("/{thread_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_thread(store: Store, thread_id: UUID) -> None:
    await store.unarchive(thread_id)


@threads.post("/{thread_id}/commands")
async def thread_command(
    store: Store,
    content: Content,
    bridge: runner_bridge.Bridge,
    catalog: Models,
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
        not command.change_model.model
        or thread.harness is None
        or command.change_model.model not in catalog.harnesses[thread.harness]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="model is incompatible with this thread's harness"
        )
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
    return sse_response(
        shutdown.until(stream.follow(event_logs, updates.changes[Channel.THREADS], thread_id, after_cursor=cursor))
    )


# Each answers its status with the exception's own text as the detail.
_ERROR_STATUS: dict[type[Exception], int] = {
    ThreadScopeChangedError: status.HTTP_410_GONE,
    ThreadEvidenceNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidPageTokenError: status.HTTP_400_BAD_REQUEST,
    ThreadNotFoundError: status.HTTP_404_NOT_FOUND,
    CommandIdConflictError: status.HTTP_409_CONFLICT,
    SandboxNotFoundError: status.HTTP_404_NOT_FOUND,
    SandboxRunningError: status.HTTP_409_CONFLICT,
    SandboxConflictError: status.HTTP_409_CONFLICT,
    SandboxTearingDownError: status.HTTP_409_CONFLICT,
    InvalidSandboxError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    BindingNotFoundError: status.HTTP_404_NOT_FOUND,
    FluxOwnedBindingError: status.HTTP_409_CONFLICT,
    SandboxOwnedBindingError: status.HTTP_409_CONFLICT,
    # 422 rather than 404: the sandbox in the path is there, and 404 on these routes already
    # says it is not. The body parsed and named something that does not resolve.
    UnknownPolicySetError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SandboxNotReachableError: status.HTTP_409_CONFLICT,
    runner_bridge.RunnerAdmissionTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    # The runner or the Sandbox's state refused a lifecycle request or a command: an unknown session.
    RefusedError: status.HTTP_409_CONFLICT,
    # The Thread's history or retained Session spec does not admit it: a spec mismatch.
    runner_bridge.ThreadStateError: status.HTTP_409_CONFLICT,
    runner_bridge.MalformedMessageError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _detail_with_status(code: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    async def handler(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=code)

    return handler


def create_app(
    inventory: SandboxServiceClient | SandboxAccess,
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
    *,
    electric: ElectricProxy,
    event_logs: EventLogStore,
    content: ContentStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: FilePath,
    system_status: SystemStatusChecker,
) -> FastAPI:
    """The whole HTTP surface, guarded. Each of `oidc` and `reviewer` enables one way to authenticate,
    and an app given neither answers 401 to everything but /healthz."""
    if set(catalog.harnesses) != set(Harness) or not any(catalog.harnesses.values()):
        raise ValueError(f"the model catalog needs every harness key and at least one offered model: {catalog=}")
    configured_presets = presets or PresetCatalog()
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
    async def service_error(request: Request, error: ServiceError) -> JSONResponse:
        code = {
            grpc.StatusCode.NOT_FOUND: 404,
            grpc.StatusCode.FAILED_PRECONDITION: 409,
            grpc.StatusCode.ALREADY_EXISTS: 409,
            grpc.StatusCode.INVALID_ARGUMENT: 422,
        }.get(error.code, 503)
        return JSONResponse({"detail": str(error)}, status_code=code)

    @app.exception_handler(ConnectionError)
    async def upstream_unavailable(request: Request, error: ConnectionError) -> JSONResponse:
        return JSONResponse({"detail": "Upstream service unavailable"}, status_code=503)

    @app.exception_handler(TimeoutError)
    async def upstream_deadline(request: Request, error: TimeoutError) -> JSONResponse:
        return JSONResponse({"detail": "Upstream deadline expired"}, status_code=504)

    for exception, code in _ERROR_STATUS.items():
        app.add_exception_handler(exception, _detail_with_status(code))

    services = AppServices(
        inventory=inventory,
        bridge=bridge,
        store=store,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
        system_status=system_status,
        models=catalog,
        presets=configured_presets,
        egress=egress,
        action_policy=action_policy,
        decisions=decisions,
        live=live,
        oidc=oidc,
        reviewer=reviewer,
        operator_actions=operator_actions,
        electric=electric,
    )
    app.state.services = services
    # Every route needs a caller. There is no unauthenticated path into the API: /healthz is
    # declared below, outside these routers.
    for api_router in (
        router,
        models,
        preset_router,
        runner_bridge.router,
        threads,
        actions_router,
        push_router,
        consent_router,
        connections_router,
        policy_binding_router,
        action_policy_router,
        live_router,
        electric_router,
        system_router,
    ):
        app.include_router(api_router, dependencies=[Depends(require_caller)])
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
        # Unguarded, because these are how a browser with no credential acquires one.
        app.include_router(auth_routes.router)
    # Outermost, so a request the drain refuses touches nothing below it.
    app.add_middleware(DrainMiddleware, drain=services.drain, liveness_path="/healthz")

    @app.get("/healthz", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT)
    async def healthz() -> None:
        # The Deployment's liveness probe: the process serves; the inventory's own reachability is
        # per request. Answered through the drain, unlike everything else.
        pass

    @app.get("/readyz", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT)
    async def readyz() -> None:
        # The Deployment's readiness probe: the drain middleware answers 503 here once shutdown begins.
        pass

    return app
