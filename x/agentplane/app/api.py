"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Annotated
from uuid import UUID

import grpc
import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, field_validator

from x.agentplane.action_service.client import OperatorActionServiceClient
from x.agentplane.action_service.connections import Connection, ConnectionRename, ConnectionVersion, Identity
from x.agentplane.action_service.enrollments import EnrollmentDecisionResult
from x.agentplane.action_service.mcp_linkage import McpLinkageStart, McpLinkageStartView, McpLinkageView
from x.agentplane.action_service.models import ActionEventView, ActionRequestView, ActionState, DecisionInput
from x.agentplane.app import auth_routes, bridge as runner_bridge
from x.agentplane.app.action_federation import (
    FederatedOperatorActions,
    OperatorFederationError,
    upstream_failure_detail,
)
from x.agentplane.app.consent import (
    ConsentDecision,
    ConsentPreview,
    EnrollmentHandle,
    decide_enrollment,
    preview_enrollment,
)
from x.agentplane.app.decisions import Decision, DecisionsClient, DecisionsUnavailableError
from x.agentplane.app.egress import (
    BindingNotFoundError,
    BindingView,
    EgressInventory,
    FluxOwnedBindingError,
    PolicyView,
    UnknownPolicyError,
)
from x.agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer, require_caller
from x.agentplane.app.inventory import (
    PRESET_BINDING_ANNOTATION,
    NewSandbox,
    SandboxInventory,
    SandboxNotFoundError,
    SandboxRunningError,
    SandboxView,
)
from x.agentplane.app.live import LiveIndex, router as live_router
from x.agentplane.app.oidc import OIDCSettings, build_oauth, operator_session
from x.agentplane.app.operator_sessions import OperatorSessionMiddleware
from x.agentplane.app.presets import (
    PresetCatalog,
    Provider,
    SandboxBinding,
    SandboxPresetView,
    ThreadDefaults,
    UnknownPresetError,
)
from x.agentplane.app.shutdown import Drain, DrainMiddleware, Shutdown
from x.agentplane.app.trajectory import ThreadNotFoundError, ThreadView, TrajectoryStore
from x.agentplane.runner.client import RunnerError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
logger = logging.getLogger(__name__)


class InvalidLaunchError(Exception):
    """A syntactically valid launch combines preset fields that cannot apply."""


# The models each harness may be opened with: the app's configuration, offered to the session form.
# A thread carries its harness and model; a sandbox is a Pod and carries neither.
ModelCatalog = dict[Provider, list[str]]


def _models(request: Request) -> ModelCatalog:
    models = request.app.state.models
    if not isinstance(models, dict):
        raise TypeError(f"app.state.models is {type(models).__name__}, not a dict")
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


def _inventory(request: Request) -> SandboxInventory:
    inventory = request.app.state.inventory
    if not isinstance(inventory, SandboxInventory):
        raise TypeError(f"app.state.inventory is {type(inventory).__name__}, not SandboxInventory")
    return inventory


Inventory = Annotated[SandboxInventory, Depends(_inventory)]


def _egress(request: Request) -> EgressInventory:
    egress = request.app.state.egress
    if not isinstance(egress, EgressInventory):
        raise TypeError(f"app.state.egress is {type(egress).__name__}, not EgressInventory")
    return egress


Egress = Annotated[EgressInventory, Depends(_egress)]


def _decisions(request: Request) -> DecisionsClient:
    decisions = request.app.state.decisions
    if not isinstance(decisions, DecisionsClient):
        raise TypeError(f"app.state.decisions is {type(decisions).__name__}, not DecisionsClient")
    return decisions


Decisions = Annotated[DecisionsClient, Depends(_decisions)]


@router.get("")
async def list_sandboxes(
    inventory: Inventory, include_archived: Annotated[bool, Query(description="Also list archived sandboxes.")] = False
) -> list[SandboxView]:
    return await inventory.list_sandboxes(include_archived=include_archived)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(inventory: Inventory, egress: Egress, presets: Presets, spec: NewSandbox) -> SandboxView:
    """Resolve an optional app preset, then create the same concrete Sandbox the no-preset API does."""
    template_name: str | None = None
    annotations: dict[str, str] | None = None
    picked_policies = spec.policies
    if spec.preset is not None:
        preset = presets.sandbox(spec.preset)
        template_name = preset.template
        picked_policies = preset.policies if "policies" not in spec.model_fields_set else spec.policies
        # Validate and preserve only explicit Sandbox-level edits; current preset defaults remain live.
        overrides = spec.thread_defaults or ThreadDefaults()
        if spec.thread_preset is not None:
            presets.thread(spec.thread_preset)
        binding = SandboxBinding(
            sandbox_preset=spec.preset, thread_preset=spec.thread_preset, thread_overrides=overrides
        )
        annotations = {PRESET_BINDING_ANNOTATION: binding.model_dump_json(exclude_none=True)}
    elif spec.thread_preset is not None or spec.thread_defaults is not None:
        raise InvalidLaunchError("thread_preset and thread_defaults require a sandbox preset")
    policies = egress.launch_policies(picked_policies)
    await egress.require_policies(policies)
    view = await inventory.create(spec, template_name=template_name, annotations=annotations)
    if policies:
        await egress.grant(sandbox=view.name, sandbox_uid=view.uid, policies=policies)
    return view


@router.get("/{name}")
async def get_sandbox(inventory: Inventory, name: str) -> SandboxView:
    return await inventory.get(name)


@router.post("/{name}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.suspend(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.resume(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.archive(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_sandbox(inventory: Inventory, name: str) -> Response:
    await inventory.unarchive(name)
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
    """What may leave the sandbox: the bindings naming it, with their policies as they resolve."""
    await inventory.require_known(name)
    return await egress.bindings_for(name)


@router.post("/{name}/egress", status_code=status.HTTP_201_CREATED)
async def grant_sandbox_egress(inventory: Inventory, egress: Egress, name: str, body: EgressGrant) -> BindingView:
    """Grant policies to a sandbox already running: a new binding naming it, never an edit of one it
    has, so this grant's expiry and revocation are its own."""
    view = await inventory.get(name)
    return await egress.grant(sandbox=view.name, sandbox_uid=view.uid, policies=body.policies)


@router.get("/{name}/egress/decisions")
async def sandbox_egress_decisions(inventory: Inventory, decisions: Decisions, name: str) -> list[Decision]:
    """What recently left or was refused, from the proxy; 502 when the proxy cannot be asked."""
    await inventory.require_known(name)
    return await decisions.recent(name)


egress_router = APIRouter(prefix="/egress", tags=["egress"])


@egress_router.get("/policies")
async def list_policies(egress: Egress) -> list[PolicyView]:
    """The namespace's policies: what the create form offers to pick from."""
    return await egress.list_policies()


@egress_router.delete("/bindings/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_binding(egress: Egress, name: str) -> Response:
    """Revoke a runtime binding by deleting the rule; one from git is refused with 409."""
    await egress.revoke(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


threads = APIRouter(prefix="/threads", tags=["threads"])


def _store(request: Request) -> TrajectoryStore:
    store = request.app.state.store
    if not isinstance(store, TrajectoryStore):
        raise TypeError(f"app.state.store is {type(store).__name__}, not TrajectoryStore")
    return store


Store = Annotated[TrajectoryStore, Depends(_store)]


actions_router = APIRouter(prefix="/actions", tags=["actions"])
push_router = APIRouter(prefix="/push", tags=["push"])
consent_router = APIRouter(prefix="/connection-enrollments", tags=["connections"])


async def _operator_actions(
    request: Request, caller: Annotated[CallerIdentity, Depends(require_caller)]
) -> AsyncIterator[OperatorActionServiceClient]:
    if caller.kind is not CallerKind.OPERATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Action Service management requires an operator session")
    provider = request.app.state.operator_actions
    if provider is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": "operator_federation_not_configured"})
    if not isinstance(provider, FederatedOperatorActions):
        raise TypeError("operator_actions must be FederatedOperatorActions")
    session = operator_session(request)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "operator_reauthentication_required")
    try:
        yield provider.for_session(session)
    except OperatorFederationError as error:
        raise HTTPException(error.status_code, {"code": str(error)}) from None
    except httpx.HTTPStatusError as error:
        raise upstream_http_error(error) from error
    except httpx.RequestError as error:
        raise upstream_http_error(error) from error


def upstream_http_error(error: httpx.HTTPStatusError | httpx.RequestError) -> HTTPException:
    """Describe the failed request, which may be to the identity provider or the service."""
    detail = upstream_failure_detail(error)
    response_status = detail["upstream_status"]
    return HTTPException(
        response_status if isinstance(response_status, int) else status.HTTP_503_SERVICE_UNAVAILABLE, detail
    )


OperatorActions = Annotated[OperatorActionServiceClient, Depends(_operator_actions)]


@consent_router.post("/{handle}/preview")
async def connection_preview(request: Request, handle: EnrollmentHandle, client: OperatorActions) -> ConsentPreview:
    return await preview_enrollment(request, handle, client)


@consent_router.post("/{handle}/decision")
async def connection_decision(
    request: Request, handle: EnrollmentHandle, body: ConsentDecision, client: OperatorActions
) -> EnrollmentDecisionResult:
    return await decide_enrollment(request, handle, body, client)


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


@connections_router.post("/mcp-servers/{server_id}/linkage/start")
async def start_mcp_linkage(server_id: str, body: McpLinkageStart, client: OperatorActions) -> McpLinkageStartView:
    return await client.start_mcp_linkage(server_id, body.scopes)


@connections_router.post("/mcp-servers/{server_id}/linkage/disconnect")
async def disconnect_mcp_linkage(server_id: str, client: OperatorActions) -> McpLinkageView:
    return await client.disconnect_mcp_linkage(server_id)


@connections_router.get("/connection-identities")
async def connection_identities(client: OperatorActions) -> dict[str, Identity]:
    return await client.list_identities()


@connections_router.get("/connections")
async def list_connections(client: OperatorActions) -> list[Connection]:
    return await client.connections()


@connections_router.get("/connections/{connection_id}")
async def get_connection(connection_id: UUID, client: OperatorActions) -> Connection:
    return await client.connection(connection_id)


@connections_router.patch("/connections/{connection_id}")
async def rename_connection(connection_id: UUID, body: ConnectionRename, client: OperatorActions) -> Connection:
    return await client.rename_connection(connection_id, body)


@connections_router.post("/connections/{connection_id}/unbind")
async def unbind_connection(connection_id: UUID, body: ConnectionVersion, client: OperatorActions) -> Connection:
    return await client.unbind_connection(connection_id, body)


@actions_router.get("")
async def list_actions(
    client: OperatorActions,
    state: Annotated[list[ActionState] | None, Query(description="Only requests in these states.")] = None,
) -> list[ActionRequestView]:
    return await client.list_requests(states=tuple(state or ()))


async def _action_chunks(client: OperatorActions) -> AsyncIterator[AsyncIterator[bytes]]:
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(30):
                chunks = await stack.enter_async_context(client.stream_requests())
        except TimeoutError as error:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Action stream startup timed out") from error
        yield chunks


@actions_router.get("/stream")
async def action_stream(
    request: Request, shutdown: Shutdown, chunks: Annotated[AsyncIterator[bytes], Depends(_action_chunks)]
) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        # Force periodic reauthentication (including logout in another replica), not state polling.
        try:
            async with asyncio.timeout(30):
                async for chunk in shutdown.until(chunks):
                    if operator_session(request) is None or await request.is_disconnected():
                        return
                    yield chunk
        except TimeoutError:
            return
        except httpx.RequestError:
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
    session_id: Annotated[str | None, Query(description="Only threads of this session id.")] = None,
) -> list[ThreadView]:
    """Every persisted thread, newest first; a thread outlives its sandbox. Both filters together
    name at most one thread: a session's."""
    return await store.list_threads(sandbox=sandbox, session_id=session_id)


@threads.get("/{thread_id}")
async def get_thread(store: Store, thread_id: UUID) -> ThreadView:
    view = await store.get_thread(thread_id)
    if view is None:
        raise ThreadNotFoundError(thread_id)
    return view


@threads.patch("/{thread_id}")
async def rename_thread(store: Store, thread_id: UUID, body: ThreadRename) -> ThreadView:
    return await store.rename(thread_id, body.name)


@threads.get("/{thread_id}/events")
async def thread_events(
    store: Store,
    thread_id: UUID,
    after: Annotated[int, Query(ge=0, description="Events with a greater sequence.")] = 0,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 10_000,
) -> list[dict[str, object]]:
    """The stored events as proto-JSON of the runner protocol's Event, in sequence order."""
    if await store.get_thread(thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    return [MessageToDict(event) for event in await store.events(thread_id, after_sequence=after, limit=limit)]


def create_app(
    inventory: SandboxInventory,
    bridge: runner_bridge.RunnerBridge,
    store: TrajectoryStore,
    catalog: ModelCatalog,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live: LiveIndex,
    oidc: OIDCSettings | None = None,
    reviewer: TokenReviewer | None = None,
    presets: PresetCatalog | None = None,
    operator_actions: FederatedOperatorActions | None = None,
) -> FastAPI:
    """The whole HTTP surface, guarded. Each of `oidc` and `reviewer` enables one way to authenticate,
    and an app given neither answers 401 to everything but /healthz."""
    if set(catalog) != set(Provider) or not all(catalog.values()):
        raise ValueError(f"the model catalog needs a non-empty list for every provider: {catalog=}")
    configured_presets = presets or PresetCatalog()
    for name, preset in configured_presets.threads.items():
        if preset.model not in catalog[preset.provider]:
            raise ValueError(f"ThreadPreset {name!r} names model {preset.model!r} outside the configured catalog")
    app = FastAPI(title="Agentplane", version="0")
    app.state.inventory = inventory
    app.state.bridge = bridge
    app.state.store = store
    app.state.models = catalog
    app.state.presets = configured_presets
    app.state.egress = egress
    app.state.decisions = decisions
    app.state.live = live
    app.state.oidc = oidc
    app.state.reviewer = reviewer
    app.state.operator_actions = operator_actions
    app.state.drain = Drain()
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
        egress_router,
        live_router,
    ):
        app.include_router(api_router, dependencies=[Depends(require_caller)])
    if oidc is not None:
        app.add_middleware(
            OperatorSessionMiddleware,
            store=store.operator_sessions,
            secret_key=oidc.session_secret,
            session_cookie=oidc.cookie_name,
            https_only=oidc.secure,
            max_age=oidc.session_seconds,
        )
        app.state.oauth = build_oauth(oidc)
        # Unguarded, because these are how a browser with no credential acquires one.
        app.include_router(auth_routes.router)
    # Outermost, so a request the drain refuses touches nothing below it.
    app.add_middleware(DrainMiddleware, drain=app.state.drain, liveness_path="/healthz")

    @app.exception_handler(ThreadNotFoundError)
    async def _thread_not_found(_request: Request, error: ThreadNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

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

    @app.exception_handler(UnknownPresetError)
    async def _unknown_preset(_request: Request, error: UnknownPresetError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(InvalidLaunchError)
    async def _invalid_launch(_request: Request, error: InvalidLaunchError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(UnknownPolicyError)
    async def _unknown_policy(_request: Request, error: UnknownPolicyError) -> JSONResponse:
        # 422 rather than 404: the sandbox in the path is there, and 404 on these routes already
        # says it is not. The body parsed and named something that does not resolve.
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.SandboxNotReachableError)
    async def _not_reachable(_request: Request, error: runner_bridge.SandboxNotReachableError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(grpc.aio.AioRpcError)
    async def _runner_unavailable(_request: Request, error: grpc.aio.AioRpcError) -> JSONResponse:
        # The Pod has an address but nothing answers on it yet: a runner still starting after a
        # resume, or one that just died. Any other gRPC failure is a bug and stays a 500.
        if error.code() != grpc.StatusCode.UNAVAILABLE:
            raise error
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": f"the sandbox's runner is not answering: {error.details()}"},
        )

    @app.exception_handler(RunnerError)
    async def _runner_refused(_request: Request, error: RunnerError) -> JSONResponse:
        # The runner refused an Open or a command: an unknown session, a spec mismatch, a bad cursor.
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.MalformedMessageError)
    async def _malformed(_request: Request, error: runner_bridge.MalformedMessageError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    return app
