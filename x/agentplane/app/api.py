"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import grpc
import httpx
import httpx2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from x.agentplane.action_service.client import OperatorActionServiceClient
from x.agentplane.action_service.connections import Connection, ConnectionRename, ConnectionVersion
from x.agentplane.action_service.enrollments import EnrollmentDecisionResult
from x.agentplane.action_service.mcp_linkage import McpLinkageStart, McpLinkageStartView, McpLinkageView
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestView,
    ActionState,
    DecisionInput,
    ServiceAccountRef,
)
from x.agentplane.app import auth_routes, bridge as runner_bridge
from x.agentplane.app.action_federation import (
    FederatedOperatorActions,
    OperatorFederationError,
    operator_actions,
    upstream_failure_detail,
)
from x.agentplane.app.action_policy import (
    ActionPolicyInventory,
    ActionPolicySetView,
    ActionPolicyView,
    UnknownPolicySetError,
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
from x.agentplane.app.identity import CallerIdentity, TokenReviewer, require_caller
from x.agentplane.app.inventory import (
    SANDBOX_BINDING_ANNOTATION,
    NewSandbox,
    SandboxCreationSpec,
    SandboxInventory,
    SandboxNotFoundError,
    SandboxRunningError,
    SandboxView,
)
from x.agentplane.app.live import LiveIndex, router as live_router
from x.agentplane.app.oidc import OIDCSettings, build_oauth, operator_session
from x.agentplane.app.operator_sessions import OperatorSessionMiddleware
from x.agentplane.app.presets import Harness, PresetCatalog, SandboxBinding, SandboxPresetView, ThreadDefaults
from x.agentplane.app.shutdown import Drain, DrainMiddleware, Shutdown
from x.agentplane.app.trajectory import (
    ExistingSandboxTarget,
    NewSandboxTarget,
    NewThreadStartRequest,
    ThreadCommandConflictError,
    ThreadCommandSnapshot,
    ThreadNotFoundError,
    ThreadStartRequestConflictError,
    ThreadView,
    TrajectoryStore,
)
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
logger = logging.getLogger(__name__)


# The models each agent harness may be opened with: the app's configuration, offered to the session form.
# A thread carries its harness and model; a sandbox is a Pod and carries neither.
ModelCatalog = dict[Harness, list[str]]


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
    return await inventory.list_sandboxes()


@router.get("/templates")
async def list_templates(inventory: Inventory) -> list[str]:
    """The templates the operator may select in a concrete new-Sandbox request."""
    return await inventory.list_templates()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(
    inventory: Inventory, egress: Egress, action_policy: ActionPolicy, spec: NewSandbox
) -> SandboxView:
    """Create exactly the fields the caller selected; browser presets have already filled them."""
    policies = egress.launch_policies(spec.policies)
    await egress.require_policies(policies)
    await action_policy.require_policy_sets(spec.action_policy_sets)
    binding = (
        SandboxBinding(thread_defaults=spec.thread_defaults, bootstrap=spec.bootstrap)
        if spec.thread_defaults is not None or spec.bootstrap
        else None
    )
    annotations = (
        {SANDBOX_BINDING_ANNOTATION: binding.model_dump_json(exclude_none=True)} if binding is not None else None
    )
    view = await inventory.create(spec, annotations=annotations)
    if policies:
        await egress.grant(sandbox=view.name, sandbox_uid=view.uid, policies=policies)
    if spec.action_policy_sets:
        await action_policy.bind(sandbox=view.name, sandbox_uid=view.uid, policy_sets=spec.action_policy_sets)
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


@router.get("/{name}/action-policy")
async def sandbox_action_policy(
    inventory: Inventory, action_policy: ActionPolicy, client: OperatorActions, name: str
) -> ActionPolicyView:
    """What the Action Service auto-decides for the sandbox, as the service resolves it now for the
    UID its bindings pin: its unexpired bindings, the sets they name, and the lists that result.
    Read-only, and an operator's read; kubectl edits the objects."""
    return await action_policy.for_sandbox(client, (await inventory.get(name)).uid)


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


action_policy_router = APIRouter(prefix="/action-policy", tags=["action-policy"])


@action_policy_router.get("/sets")
async def list_policy_sets(action_policy: ActionPolicy) -> list[ActionPolicySetView]:
    """The namespace's sets with the Action Service's verdict on each: what a launch picks from."""
    return await action_policy.list_policy_sets()


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


@connections_router.get("/connection-service-accounts")
async def connection_service_accounts(client: OperatorActions) -> list[ServiceAccountRef]:
    return await client.caller_service_accounts()


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
        except httpx.RequestError, httpx2.HTTPStatusError, httpx2.TransportError:
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


class ThreadCommandView(BaseModel):
    """An accepted durable desired command; actual receipt/effect remains in runner Events."""

    model_config = ConfigDict(extra="forbid")

    command: dict[str, object]
    ordinal: int
    accepted_at: datetime


def _thread_command_view(snapshot: ThreadCommandSnapshot) -> ThreadCommandView:
    return ThreadCommandView(
        command=MessageToDict(snapshot.command), ordinal=snapshot.ordinal, accepted_at=snapshot.accepted_at
    )


def _thread_command(body: dict[str, object]) -> pb.Command:
    """Accept the runner protocol's canonical Command JSON as durable app intent."""
    try:
        command = ParseDict(body, pb.Command())
    except ParseError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"not a Command: {error}") from error
    if not command.command_id or command.WhichOneof("operation") is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "a Thread command needs a command id and exactly one operation"
        )
    return command


class ExistingThreadStartTarget(BaseModel):
    """Start a new Thread in the exact live Sandbox selected by the operator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["existing"]
    sandbox: str = Field(min_length=1)


class NewThreadStartTarget(BaseModel):
    """Create a Sandbox from concrete form fields, then start its first Thread."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["new"]
    sandbox: NewSandbox


ThreadStartTarget = Annotated[ExistingThreadStartTarget | NewThreadStartTarget, Field(discriminator="kind")]


class FirstThreadInput(BaseModel):
    """The first desired SubmitInput; its command id is durable outbox identity."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)


class NewThreadStart(BaseModel):
    """A client-minted product Thread, its target, planned runner session, and first input."""

    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    runner_session_id: str = Field(min_length=1, max_length=200)
    target: ThreadStartTarget
    thread_overrides: ThreadDefaults = Field(default_factory=ThreadDefaults)
    first_input: FirstThreadInput


def _resolve_thread_spec(
    *,
    inherited: ThreadDefaults,
    overrides: ThreadDefaults,
    runner_session_id: str,
    presets: PresetCatalog,
    catalog: ModelCatalog,
) -> pb.SessionSpec:
    """Resolve editable Thread fields now, before a durable target can outlive current presets."""
    try:
        spec = ParseDict(overrides.over(inherited).proto_json(runner_session_id), pb.SessionSpec())
    except ParseError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid Thread fields: {error}") from error
    if spec.harness not in (pb.HARNESS_CLAUDE, pb.HARNESS_CODEX) or not spec.model or not spec.cwd:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "a Thread without inherited defaults needs harness, model, and cwd"
        )
    harness = Harness(pb.Harness.Name(spec.harness))
    if spec.model not in catalog[harness]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"model {spec.model!r} is not available for {harness}"
        )
    spec.instructions = presets.instructions_for(spec.instructions)
    return spec


async def _sandbox_creation(
    spec: NewSandbox, *, egress: EgressInventory, action_policy: ActionPolicyInventory
) -> SandboxCreationSpec:
    """Validate and freeze concrete new-Sandbox fields; presets are never retained as desired state."""
    policies = egress.launch_policies(spec.policies)
    await egress.require_policies(policies)
    await action_policy.require_policy_sets(spec.action_policy_sets)
    return SandboxCreationSpec(
        slug=spec.slug,
        template=spec.template,
        egress_policies=policies,
        action_policy_sets=spec.action_policy_sets,
        thread_defaults=spec.thread_defaults,
        bootstrap=spec.bootstrap,
    )


async def _start_thread_request(
    body: NewThreadStart,
    *,
    store: TrajectoryStore,
    inventory: SandboxInventory,
    egress: EgressInventory,
    action_policy: ActionPolicyInventory,
    presets: PresetCatalog,
    catalog: ModelCatalog,
) -> ThreadView:
    # A retry after response loss must read the immutable accepted intent without consulting a
    # changed preset, inventory, or policy set. The store guards conflicting reuse at its boundary.
    if await store.thread_start_request(body.thread_id) is not None:
        existing = await store.get_thread(body.thread_id)
        if existing is None:  # pragma: no cover - the request has a foreign key to Thread.
            raise RuntimeError("Thread start request has no product Thread")
        return existing

    target: ExistingSandboxTarget | NewSandboxTarget
    match body.target:
        case ExistingThreadStartTarget(sandbox=name):
            sandbox = await inventory.get(name)
            inherited = (
                sandbox.binding.thread_defaults
                if sandbox.binding is not None and sandbox.binding.thread_defaults is not None
                else ThreadDefaults()
            )
            target = ExistingSandboxTarget(sandbox.name, sandbox.uid)
        case NewThreadStartTarget(sandbox=new_sandbox):
            creation = await _sandbox_creation(new_sandbox, egress=egress, action_policy=action_policy)
            inherited = creation.thread_defaults or ThreadDefaults()
            target = NewSandboxTarget(creation)
    spec = _resolve_thread_spec(
        inherited=inherited,
        overrides=body.thread_overrides,
        runner_session_id=body.runner_session_id,
        presets=presets,
        catalog=catalog,
    )
    await store.request_thread_start(
        NewThreadStartRequest(
            thread_id=body.thread_id,
            target=target,
            runner_session_id=body.runner_session_id,
            session_spec=spec,
            first_input=pb.Command(
                command_id=body.first_input.command_id, submit_input=pb.SubmitInput(text=body.first_input.text)
            ),
        )
    )
    created = await store.get_thread(body.thread_id)
    if created is None:  # pragma: no cover - request_thread_start created it transactionally.
        raise RuntimeError("new Thread disappeared before its response")
    return created


@threads.post("", status_code=status.HTTP_201_CREATED)
async def start_thread(
    body: NewThreadStart,
    store: Store,
    inventory: Inventory,
    egress: Egress,
    action_policy: ActionPolicy,
    presets: Presets,
    catalog: Annotated[ModelCatalog, Depends(_models)],
) -> ThreadView:
    """Persist first Thread input in the outbox before its Sandbox or runner exists."""
    return await _start_thread_request(
        body,
        store=store,
        inventory=inventory,
        egress=egress,
        action_policy=action_policy,
        presets=presets,
        catalog=catalog,
    )


@threads.get("")
async def list_threads(
    store: Store,
    sandbox: Annotated[str | None, Query(description="Only threads of this sandbox.")] = None,
    session_id: Annotated[str | None, Query(description="Only threads of this session id.")] = None,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
) -> list[ThreadView]:
    """Every persisted thread, newest first; a thread outlives its sandbox. Both filters together
    name at most one thread: a session's."""
    return await store.list_threads(sandbox=sandbox, session_id=session_id, include_archived=include_archived)


@threads.get("/{thread_id}/commands")
async def thread_commands(store: Store, thread_id: UUID) -> list[ThreadCommandView]:
    """Desired commands in durable per-Thread order, before their runner receipt/effect Events."""
    if await store.get_thread(thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    return [_thread_command_view(command) for command in await store.thread_commands(thread_id)]


@threads.post("/{thread_id}/commands", status_code=status.HTTP_202_ACCEPTED)
async def request_thread_command(
    store: Store, thread_id: UUID, body: dict[str, object], catalog: Annotated[ModelCatalog, Depends(_models)]
) -> ThreadCommandView:
    """Commit a generic desired runner command; a reconciler delivers it later."""
    command = _thread_command(body)
    if command.HasField("change_model"):
        thread = await store.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)
        if not command.change_model.model or command.change_model.model not in catalog[thread.harness]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "model is incompatible with this Thread's harness"
            )
    return _thread_command_view(await store.request_thread_command(thread_id, command))


class ThreadsWithSandboxes(BaseModel):
    """Every Thread plus each attached Thread's still-existing Sandbox, keyed by name.

    A target-only Thread remains visible with null attachment fields. An attached Thread whose
    non-null Sandbox name is absent from `sandboxes` belongs to a deleted Sandbox.
    """

    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadView]
    sandboxes: dict[str, SandboxView]


@threads.get("/with-sandboxes")
async def list_threads_with_sandboxes(
    store: Store,
    inventory: Inventory,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
) -> ThreadsWithSandboxes:
    """Every Thread, newest first, with each attached Thread's still-existing Sandbox once."""
    thread_views = await store.list_threads(include_archived=include_archived)
    referenced = {thread.sandbox for thread in thread_views if thread.sandbox is not None}
    sandboxes = {view.name: view for view in await inventory.list_sandboxes() if view.name in referenced}
    return ThreadsWithSandboxes(threads=thread_views, sandboxes=sandboxes)


@threads.get("/{thread_id}")
async def get_thread(store: Store, thread_id: UUID) -> ThreadView:
    view = await store.get_thread(thread_id)
    if view is None:
        raise ThreadNotFoundError(thread_id)
    return view


@threads.patch("/{thread_id}")
async def rename_thread(store: Store, thread_id: UUID, body: ThreadRename) -> ThreadView:
    return await store.rename(thread_id, body.name)


@threads.post("/{thread_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_thread(store: Store, thread_id: UUID) -> Response:
    await store.archive(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@threads.post("/{thread_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_thread(store: Store, thread_id: UUID) -> Response:
    await store.unarchive(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    action_policy: ActionPolicyInventory,
    oidc: OIDCSettings | None = None,
    reviewer: TokenReviewer | None = None,
    presets: PresetCatalog | None = None,
    operator_actions: FederatedOperatorActions | None = None,
) -> FastAPI:
    """The whole HTTP surface, guarded. Each of `oidc` and `reviewer` enables one way to authenticate,
    and an app given neither answers 401 to everything but /healthz."""
    if set(catalog) != set(Harness) or not all(catalog.values()):
        raise ValueError(f"the model catalog needs a non-empty list for every harness: {catalog=}")
    configured_presets = presets or PresetCatalog()
    for name, preset in configured_presets.threads.items():
        if preset.model not in catalog[preset.harness]:
            raise ValueError(f"ThreadPreset {name!r} names model {preset.model!r} outside the configured catalog")
    app = FastAPI(title="Agentplane", version="0")
    app.state.inventory = inventory
    app.state.bridge = bridge
    app.state.store = store
    app.state.models = catalog
    app.state.presets = configured_presets
    app.state.egress = egress
    app.state.action_policy = action_policy
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
        action_policy_router,
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

    @app.exception_handler(ThreadCommandConflictError)
    @app.exception_handler(ThreadStartRequestConflictError)
    async def _thread_command_conflict(
        _request: Request, error: ThreadCommandConflictError | ThreadStartRequestConflictError
    ) -> JSONResponse:
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
