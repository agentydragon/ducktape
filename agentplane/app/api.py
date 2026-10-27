"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

import grpc
import httpx
import httpx2
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from google.protobuf.json_format import MessageToDict
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from agentplane.action_service.policy_view import ActionPolicySetView
from agentplane.app import auth_routes
from agentplane.app.action_federation import (
    FederatedOperatorActions,
    OperatorFederationError,
    operator_action_token,
    upstream_failure_detail,
)
from agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
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
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import OIDCSettings, build_oauth
from agentplane.app.operator_relay import operator_relay
from agentplane.app.operator_sessions import OperatorSessionMiddleware, OperatorSessionStore, request_session
from agentplane.app.presets import PresetCatalog, SandboxPresetView
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.sandbox_models import (
    NewSandbox,
    NewSandboxDefaults,
    RunnerStatusView,
    SandboxIdleSuspendChange,
    SandboxView,
    SandboxWakePolicyChange,
    create_request,
    sandbox_is_ready,
    sandbox_view,
)
from agentplane.app.settings import FrontendConfig
from agentplane.app.shutdown import Drain, DrainMiddleware, Shutdown
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
from agentplane.app.trace_relay import relay_traces
from agentplane.app.upstream_proxy import UpstreamProxy, response_body
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import RefusedError, ServiceError
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.sandboxes.egress_views import (
    BindingNotFoundError,
    BindingView,
    FluxOwnedBindingError,
    SandboxOwnedBindingError,
)
from agentplane.sandboxes.errors import (
    ConsoleUnavailableError,
    InvalidSandboxError,
    SandboxConflictError,
    SandboxModeUnknownError,
    SandboxNotFoundError,
    SandboxQuiescingError,
    SandboxRunningError,
    SandboxTearingDownError,
    UnknownPolicySetError,
)
from agentplane.sse import frame, sse_response
from agentplane.subjects import ServiceAccountRef
from agentplane.tracing import instrument_fastapi

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


@dataclass
class AppServices:
    """Everything the app's routes use, built once by `create_app` and held as `app.state.services`.

    The modules below this one (`live`, `electric`, `identity`, `oidc`, `shutdown`, `action_federation`,
    `auth_routes`, `threads.bridge`) read their own field by name: they define the types this class
    names, so importing it would be a dependency cycle.
    """

    inventory: SandboxAccess
    bridge: runner_bridge.RunnerBridge
    store: ThreadStore
    event_logs: EventLogStore
    content: ContentStore
    database_updates: WakeupListener
    operator_sessions: OperatorSessionStore
    notifications: NotificationsOperatorClient
    system_status: SystemStatusChecker
    models: ModelCatalog
    presets: PresetCatalog
    new_sandbox_defaults: NewSandboxDefaults
    egress: EgressAccess
    action_policy: ActionPolicyInventory
    decisions: DecisionsClient
    live: LiveIndex
    oidc: OIDCSettings | None
    reviewer: TokenReviewer | None
    operator_actions: FederatedOperatorActions | None
    electric: ElectricProxy | None
    dispatcher_http: UpstreamProxy
    actions_http: UpstreamProxy | None
    connection_http: UpstreamProxy | None
    frontend_config: FrontendConfig = field(default_factory=FrontendConfig)
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
Inventory = Annotated[SandboxAccess, _service(lambda services: services.inventory)]
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

trace_relay_router = APIRouter(prefix="/telemetry", include_in_schema=False)


@trace_relay_router.post("/v1/traces", dependencies=[OperatorLogin])
async def browser_trace_export(request: Request) -> Response:
    """Forward same-origin browser spans to the server-configured OTLP/HTTP receiver."""
    return await relay_traces(request)


models = APIRouter(prefix="/models", tags=["models"])


@models.get("")
async def list_models(catalog: Models) -> ModelCatalog:
    return catalog


preset_router = APIRouter(prefix="/presets", tags=["presets"])
frontend_config_router = APIRouter(tags=["frontend config"])


@preset_router.get("")
async def list_presets(services: Services) -> list[SandboxPresetView]:
    return services.presets.views()


@frontend_config_router.get("/frontend-config")
async def get_frontend_config(services: Services) -> FrontendConfig:
    """Browser-facing configuration; optional settings remain present as null."""
    return services.frontend_config


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
    new_sandbox_defaults: NewSandboxDefaults


@router.get("/form")
async def sandbox_form(services: Services) -> SandboxForm:
    """What the create form offers and starts with."""
    return SandboxForm(
        policy_sets=await services.action_policy.policy_set_names(), new_sandbox_defaults=services.new_sandbox_defaults
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(spec: NewSandbox, services: Services) -> SandboxView:
    """Create exactly the fields the caller selected; browser presets have already filled them."""
    if spec.preset is not None and spec.preset not in services.presets.sandboxes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"no preset named {spec.preset!r}")
    return sandbox_view(await services.inventory.create(create_request(spec)))


@router.get("/{name}/runner-status")
async def sandbox_runner_status(bridge: runner_bridge.Bridge, name: str) -> RunnerStatusView:
    try:
        snapshot = await bridge.runner_status(name)
    except SandboxNotReachableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    return RunnerStatusView.model_validate(
        MessageToDict(snapshot, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    )


@router.get("/{name}")
async def get_sandbox(inventory: Inventory, name: str) -> SandboxView:
    return sandbox_view(await inventory.get(name))


@router.get("/{name}/notifications/stream", dependencies=[OperatorLogin])
async def sandbox_notifications_stream(
    request: Request, services: Services, name: str, shutdown: Shutdown, updates: Updates
) -> StreamingResponse:
    """Operator-only SSE of the Sandbox's notification state, scoped to the current incarnation's UID."""
    sandbox = sandbox_view(await services.inventory.get(name))
    return _operator_relay(
        request,
        shutdown,
        updates,
        services.operator_sessions,
        services.notifications.watch_sandbox_notifications(
            namespace=sandbox.namespace, name=sandbox.name, uid=sandbox.uid
        ),
        "Notification",
        (ServiceError, OSError),
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
    return sse_response(operator_relay(request, shutdown, updates, sessions, chunks, label, errors))


upstream_router = APIRouter(prefix="/upstream", tags=["upstream"])


def _generated_facade_route_allowed(method: str, path: str) -> bool:
    """Keep the proxy inside a service's versioned HTTP facade namespace."""
    segments = path.split("/")
    return (
        method in {"GET", "POST", "PATCH", "DELETE"}
        and path.startswith("v1/")
        and all(segment not in {"", ".", ".."} for segment in segments)
        and "\\" not in path
    )


@upstream_router.get("/dispatcher/{path:path}", dependencies=[OperatorLogin])
async def dispatcher_upstream(
    request: Request, services: Services, shutdown: Shutdown, updates: Updates, sessions: OperatorSessions, path: str
) -> StreamingResponse:
    """The Session Dispatcher's HTTP/JSON reads (`agentplane/runner_gateway/dispatcher/session_dispatcher.proto`), unchanged,
    for as long as the operator session lasts. Its commands are not reachable here: they go through the
    Thread routes, whose checks the Dispatcher does not make."""
    try:
        upstream = await services.dispatcher_http.open_get(path, request.url.query)
    except httpx.RequestError as error:
        raise upstream_http_error(error) from error
    return StreamingResponse(
        operator_relay(request, shutdown, updates, sessions, response_body(upstream), "Dispatcher", (httpx.HTTPError,)),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _operator_facade_upstream(
    request: Request,
    client: UpstreamProxy | None,
    label: str,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessions,
    caller: Caller,
    path: str,
) -> StreamingResponse:
    """Forward a generated facade call with this session's federated operator token.

    Each service has its own explicitly configured client. Backend HTTP mappings and
    authorization control which generated calls are available; browser credentials are never
    forwarded.
    """
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{label} is not configured")
    if not _generated_facade_route_allowed(request.method, path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown {label} route")
    try:
        bearer = await operator_action_token(request, caller)
        headers = {
            key: value
            for key in ("accept", "content-type", "user-agent")
            if (value := request.headers.get(key)) is not None
        }
        upstream = await client.open_request(
            request.method,
            path,
            request.url.query,
            bearer=bearer,
            headers=headers,
            content=await request.body() if request.method != "GET" else None,
        )
    except OperatorFederationError as error:
        raise HTTPException(error.status_code, {"code": str(error)}) from None
    except (httpx.HTTPStatusError, httpx.RequestError, httpx2.HTTPStatusError, httpx2.TransportError) as error:
        raise upstream_http_error(error) from error
    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{label} redirects are not accepted")
    return StreamingResponse(
        operator_relay(
            request,
            shutdown,
            updates,
            sessions,
            response_body(upstream),
            label,
            (httpx.HTTPError, httpx2.TransportError, OperatorFederationError),
        ),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@upstream_router.api_route(
    "/actions/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE"],
    dependencies=[OperatorLogin],
    include_in_schema=False,
)
async def actions_upstream(
    request: Request,
    services: Services,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessions,
    caller: Caller,
    path: str,
) -> StreamingResponse:
    """Relay the Action Service facade and MCP linkage callback calls from this operator session."""
    return await _operator_facade_upstream(
        request, services.actions_http, "Action facade", shutdown, updates, sessions, caller, path
    )


@upstream_router.api_route(
    "/connections/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE"],
    dependencies=[OperatorLogin],
    include_in_schema=False,
)
async def connections_upstream(
    request: Request,
    services: Services,
    shutdown: Shutdown,
    updates: Updates,
    sessions: OperatorSessions,
    caller: Caller,
    path: str,
) -> StreamingResponse:
    """Relay generated ConnectionService calls to the explicitly configured Connection endpoint."""
    return await _operator_facade_upstream(
        request, services.connection_http, "Connection facade", shutdown, updates, sessions, caller, path
    )


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


@router.patch("/{name}/wake-policy", status_code=status.HTTP_204_NO_CONTENT)
async def set_sandbox_wake_policy(inventory: Inventory, name: str, change: SandboxWakePolicyChange) -> None:
    """Change whether messages may resume this exact Sandbox incarnation; this does not wake it."""
    await inventory.set_wake_on_send(name, uid=str(change.uid), wake_on_send=change.wake_on_send)


@router.patch("/{name}/idle-suspend", status_code=status.HTTP_204_NO_CONTENT)
async def set_sandbox_idle_suspend(inventory: Inventory, name: str, change: SandboxIdleSuspendChange) -> None:
    """Change how long this exact Sandbox incarnation's runner must be idle before it is suspended."""
    await inventory.set_idle_suspend_after(
        name,
        uid=str(change.uid),
        idle_suspend_after=None
        if change.idle_suspend_after_seconds is None
        else timedelta(seconds=change.idle_suspend_after_seconds),
    )


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
    """The bindings naming the ServiceAccount the sandbox runs as, with the policy sets they name."""
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


# `{thread_id:uuid}`: the router, not only the handler, refuses a non-UUID segment, so a path no
# route serves (such as a removed route an old browser tab still calls) answers 404 rather than a
# UUID parse error from the nearest `{thread_id}` route.
threads = APIRouter(prefix="/threads", tags=["threads"])


def upstream_http_error(
    error: httpx.HTTPStatusError | httpx.RequestError | httpx2.HTTPStatusError | httpx2.TransportError,
) -> HTTPException:
    """Describe the failed request, which may be to the identity provider or the service."""
    detail = upstream_failure_detail(error)
    return HTTPException(
        detail.upstream_status if detail.upstream_status is not None else status.HTTP_503_SERVICE_UNAVAILABLE,
        detail.model_dump(),
    )


operator_router = APIRouter(tags=["operator"])
mcp_callback_router = APIRouter(tags=["connections"])


@mcp_callback_router.get("/mcp-linkage/callback")
async def complete_mcp_linkage(state: str, code: str, services: Services) -> RedirectResponse:
    """Bridge the provider's registered callback to the public Action Service callback."""
    if services.actions_http is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Action facade is not configured")
    try:
        upstream = await services.actions_http.open_request(
            "GET",
            "v1/mcp-linkage/callback",
            str(httpx.QueryParams({"state": state, "code": code})),
            bearer=None,
            headers={"accept": "application/json"},
        )
    except (httpx.RequestError, httpx2.TransportError) as error:
        raise upstream_http_error(error) from error
    try:
        if 300 <= upstream.status_code < 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Action callback redirects are not accepted")
        if not 200 <= upstream.status_code < 300:
            raise HTTPException(upstream.status_code, "MCP linkage callback failed")
    finally:
        await upstream.aclose()
    return RedirectResponse("/#/mcp-servers?linked=1", status_code=status.HTTP_303_SEE_OTHER)


class CallerGrantView(BaseModel):
    """Account-scoped CR projections, not a new grant authority or Kubernetes RBAC evaluator."""

    model_config = ConfigDict(extra="forbid")

    bindings: list[BindingView]
    action_policy: ActionPolicyView | ActionPolicyUnavailable


@operator_router.get("/caller-grants/{namespace}/{name}")
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
    return CallerGrantView(bindings=bindings, action_policy=policy)


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
    sandbox_uid: Annotated[UUID | None, Query(description="Only threads of this Sandbox incarnation.")] = None,
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
        limit=limit,
        page_token=page_token,
        sandbox=sandbox,
        sandbox_uid=None if sandbox_uid is None else str(sandbox_uid),
        search=search,
        include_archived=include_archived,
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
    # A Thread's ID is its Session's.
    return _operator_relay(
        request,
        shutdown,
        updates,
        services.operator_sessions,
        services.notifications.watch_session_notices(thread_id),
        "Notification",
        (ServiceError, OSError),
    )


@threads.get("/{thread_id:uuid}")
async def get_thread(store: Store, thread_id: UUID) -> ThreadView:
    view = await store.get_thread(thread_id)
    if view is None:
        raise ThreadNotFoundError(thread_id)
    return view


@threads.patch("/{thread_id:uuid}")
async def rename_thread(store: Store, thread_id: UUID, body: ThreadRename) -> ThreadView:
    return await store.rename(thread_id, body.name)


@threads.post("/{thread_id:uuid}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_thread(store: Store, bridge: runner_bridge.Bridge, inventory: Inventory, thread_id: UUID) -> None:
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    sandbox = None
    try:
        sandbox = sandbox_view(await inventory.get(thread.sandbox))
    except SandboxNotFoundError:
        # A deleted Sandbox has no running harness to keep visible.
        pass
    else:
        if thread.sandbox_uid is None or str(sandbox.uid) != str(thread.sandbox_uid):
            sandbox = None
    if sandbox is not None and sandbox.pod is not None:
        if not sandbox_is_ready(sandbox):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot verify the runner while a Pod exists; wait for its status to become ready or for it to be removed",
            )
        snapshot = await bridge.runner_status(thread.sandbox)
        if any(session.session_id == str(thread.id) and session.busy for session in snapshot.sessions):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "finish or interrupt active work before archiving this thread"
            )
    await store.archive(thread_id)


@threads.post("/{thread_id:uuid}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_thread(store: Store, thread_id: UUID) -> None:
    await store.unarchive(thread_id)


class DispatchedCommand(BaseModel):
    kind: Literal["dispatch"] = "dispatch"
    dispatch: dict[str, object]


class ArchivedCommandAdmission(BaseModel):
    kind: Literal["archived_admission"] = "archived_admission"
    admission: dict[str, object]


CommandSubmissionResponse = Annotated[DispatchedCommand | ArchivedCommandAdmission, Field(discriminator="kind")]


class InterruptResultSubmission(BaseModel):
    kind: Literal["interrupt_result"] = "interrupt_result"
    result: dict[str, object]


InterruptSubmissionResponse = Annotated[
    InterruptResultSubmission | ArchivedCommandAdmission, Field(discriminator="kind")
]


class CommandIdBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(alias="commandId", min_length=1)


class SubmitInputBody(CommandIdBody):
    text: str


class ChangeModelBody(CommandIdBody):
    model: str


class ChangeReasoningEffortBody(CommandIdBody):
    effort: str


class InterruptTurnBody(CommandIdBody):
    turn_id: str = Field(alias="turnId")


async def _submission_target(
    store: Store, content: Content, bridge: runner_bridge.Bridge, thread_id: UUID, journal_command: command_pb2.Command
) -> ThreadView | ArchivedCommandAdmission:
    """An exact archived retry survives Sandbox deletion; a new submission needs a live Thread."""
    if admitted := await content.admitted_command(thread_id, journal_command):
        return ArchivedCommandAdmission(admission=MessageToDict(admitted))
    thread = await store.get_thread(thread_id)
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    if thread.sandbox_uid is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the Thread's Sandbox identity is not recorded")
    await bridge.command_ready(thread_id)
    return thread


@threads.post("/{thread_id:uuid}/submit-input", response_model=CommandSubmissionResponse)
async def submit_input(
    store: Store, content: Content, bridge: runner_bridge.Bridge, thread_id: UUID, body: SubmitInputBody
) -> DispatchedCommand | ArchivedCommandAdmission:
    target = await _submission_target(
        store,
        content,
        bridge,
        thread_id,
        command_pb2.Command(command_id=body.command_id, submit_input=command_pb2.SubmitInput(text=body.text)),
    )
    if isinstance(target, ArchivedCommandAdmission):
        return target
    try:
        response = await bridge.dispatcher.unary(
            bridge.dispatcher.stub.SubmitInput,
            session_dispatcher_pb2.SubmitInputRequest(
                command_id=body.command_id,
                session_id=str(thread_id),
                sandbox=target.sandbox,
                sandbox_uid=str(target.sandbox_uid),
                text=body.text,
            ),
        )
    except TimeoutError as error:
        raise runner_bridge.DispatcherRequestTimeoutError(body.command_id) from error
    return DispatchedCommand(dispatch=MessageToDict(response.dispatch))


@threads.post("/{thread_id:uuid}/change-model", response_model=CommandSubmissionResponse)
async def change_model(
    store: Store,
    content: Content,
    bridge: runner_bridge.Bridge,
    catalog: Models,
    thread_id: UUID,
    body: ChangeModelBody,
) -> DispatchedCommand | ArchivedCommandAdmission:
    target = await _submission_target(
        store,
        content,
        bridge,
        thread_id,
        command_pb2.Command(command_id=body.command_id, change_model=command_pb2.ChangeModel(model=body.model)),
    )
    if isinstance(target, ArchivedCommandAdmission):
        return target
    if not body.model or target.harness is None or body.model not in catalog.harnesses[target.harness]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="model is incompatible with this thread's harness"
        )
    try:
        response = await bridge.dispatcher.unary(
            bridge.dispatcher.stub.ChangeModel,
            session_dispatcher_pb2.ChangeModelRequest(
                command_id=body.command_id,
                session_id=str(thread_id),
                sandbox=target.sandbox,
                sandbox_uid=str(target.sandbox_uid),
                model=body.model,
            ),
        )
    except TimeoutError as error:
        raise runner_bridge.DispatcherRequestTimeoutError(body.command_id) from error
    return DispatchedCommand(dispatch=MessageToDict(response.dispatch))


@threads.post("/{thread_id:uuid}/change-reasoning-effort", response_model=CommandSubmissionResponse)
async def change_reasoning_effort(
    store: Store, content: Content, bridge: runner_bridge.Bridge, thread_id: UUID, body: ChangeReasoningEffortBody
) -> DispatchedCommand | ArchivedCommandAdmission:
    target = await _submission_target(
        store,
        content,
        bridge,
        thread_id,
        command_pb2.Command(
            command_id=body.command_id, change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=body.effort)
        ),
    )
    if isinstance(target, ArchivedCommandAdmission):
        return target
    try:
        response = await bridge.dispatcher.unary(
            bridge.dispatcher.stub.ChangeReasoningEffort,
            session_dispatcher_pb2.ChangeReasoningEffortRequest(
                command_id=body.command_id,
                session_id=str(thread_id),
                sandbox=target.sandbox,
                sandbox_uid=str(target.sandbox_uid),
                effort=body.effort,
            ),
        )
    except TimeoutError as error:
        raise runner_bridge.DispatcherRequestTimeoutError(body.command_id) from error
    return DispatchedCommand(dispatch=MessageToDict(response.dispatch))


@threads.post("/{thread_id:uuid}/interrupt-turn", response_model=InterruptSubmissionResponse)
async def interrupt_turn(
    store: Store, content: Content, bridge: runner_bridge.Bridge, thread_id: UUID, body: InterruptTurnBody
) -> InterruptResultSubmission | ArchivedCommandAdmission:
    target = await _submission_target(
        store,
        content,
        bridge,
        thread_id,
        command_pb2.Command(command_id=body.command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=body.turn_id)),
    )
    if isinstance(target, ArchivedCommandAdmission):
        return target
    try:
        response = await bridge.dispatcher.unary(
            bridge.dispatcher.stub.InterruptTurn,
            session_dispatcher_pb2.InterruptTurnRequest(
                command_id=body.command_id,
                session_id=str(thread_id),
                sandbox=target.sandbox,
                sandbox_uid=str(target.sandbox_uid),
                turn_id=body.turn_id,
            ),
        )
    except TimeoutError as error:
        raise runner_bridge.DispatcherRequestTimeoutError(body.command_id) from error
    return InterruptResultSubmission(result=MessageToDict(response))


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


@threads.get("/{thread_id:uuid}/evidence")
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


@threads.get("/{thread_id:uuid}/evidence/{observation_cursor}/frames")
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


@threads.get("/{thread_id:uuid}/observations/{cursor}")
async def thread_observation_entry(thread_id: UUID, cursor: int, event_logs: EventLogs) -> ArchivedObservationEntry:
    """The raw entry behind one listed observation, read only when a reader expands it."""
    entry = await event_logs.observation_entry(thread_id, cursor)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no observation at {cursor} in this thread")
    return entry


@threads.get("/{thread_id:uuid}/observations")
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


@threads.get("/{thread_id:uuid}/events/stream")
async def thread_event_stream(
    store: Store,
    event_logs: EventLogs,
    updates: Updates,
    shutdown: Shutdown,
    thread_id: UUID,
    after: Annotated[int, Query(ge=0, description="Replay EventEntries with a greater cursor.")] = 0,
    awaiting_command_id: Annotated[
        str | None,
        Query(description="A command submitted after `after`: the stream does not end before its admission."),
    ] = None,
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
        shutdown.until(
            stream.follow(
                event_logs,
                updates.changes[Channel.THREADS],
                thread_id,
                after_cursor=cursor,
                awaiting_command_id=awaiting_command_id,
            )
        )
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
    SandboxModeUnknownError: status.HTTP_409_CONFLICT,
    SandboxConflictError: status.HTTP_409_CONFLICT,
    SandboxTearingDownError: status.HTTP_409_CONFLICT,
    SandboxQuiescingError: status.HTTP_503_SERVICE_UNAVAILABLE,
    InvalidSandboxError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    BindingNotFoundError: status.HTTP_404_NOT_FOUND,
    FluxOwnedBindingError: status.HTTP_409_CONFLICT,
    SandboxOwnedBindingError: status.HTTP_409_CONFLICT,
    # 422 rather than 404: the sandbox in the path is there, and 404 on these routes already
    # says it is not. The body parsed and named something that does not resolve.
    UnknownPolicySetError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SandboxNotReachableError: status.HTTP_409_CONFLICT,
    runner_bridge.DispatcherRequestTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    runner_bridge.SessionOpeningNotFoundError: status.HTTP_404_NOT_FOUND,
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
    inventory: SandboxAccess,
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
    new_sandbox_defaults: NewSandboxDefaults | None = None,
    operator_actions: FederatedOperatorActions | None = None,
    *,
    electric: ElectricProxy | None,
    dispatcher_http: UpstreamProxy,
    actions_http: UpstreamProxy | None = None,
    connection_http: UpstreamProxy | None = None,
    event_logs: EventLogStore,
    content: ContentStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    notifications: NotificationsOperatorClient,
    system_status: SystemStatusChecker,
    frontend_config: FrontendConfig | None = None,
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
            grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
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
        notifications=notifications,
        system_status=system_status,
        models=catalog,
        presets=configured_presets,
        new_sandbox_defaults=new_sandbox_defaults or NewSandboxDefaults(),
        egress=egress,
        action_policy=action_policy,
        decisions=decisions,
        live=live,
        oidc=oidc,
        reviewer=reviewer,
        operator_actions=operator_actions,
        electric=electric,
        dispatcher_http=dispatcher_http,
        actions_http=actions_http,
        connection_http=connection_http,
        frontend_config=frontend_config if frontend_config is not None else FrontendConfig(),
    )
    app.state.services = services
    # Every operator route needs a caller. The provider callback has its single-use state/code
    # contract, and /healthz is the only other unauthenticated route.
    for api_router in (
        router,
        models,
        preset_router,
        frontend_config_router,
        runner_bridge.router,
        threads,
        operator_router,
        policy_binding_router,
        action_policy_router,
        live_router,
        system_router,
        trace_relay_router,
    ):
        app.include_router(api_router, dependencies=[Depends(require_caller)])
    app.include_router(mcp_callback_router)
    if electric is not None:
        app.include_router(electric_router, dependencies=[Depends(require_caller)])
    app.include_router(upstream_router, dependencies=[Depends(require_caller)])
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

    instrument_fastapi(app)
    return app
