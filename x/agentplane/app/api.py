"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

import grpc
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from x.agentplane.app import bridge as runner_bridge
from x.agentplane.app.decisions import Decision, DecisionsClient, DecisionsUnavailableError
from x.agentplane.app.egress import (
    BindingNotFoundError,
    BindingView,
    EgressInventory,
    FluxOwnedBindingError,
    PolicyView,
)
from x.agentplane.app.inventory import LabelValue, NewSandbox, SandboxInventory, SandboxNotFoundError, SandboxView
from x.agentplane.app.trajectory import ThreadNotFoundError, ThreadView, TrajectoryStore
from x.agentplane.runner.client import RunnerError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


class Provider(StrEnum):
    """The harness a session runs; the runner image carries both, so a sandbox is not tied to one."""

    CLAUDE = "claude"
    CODEX = "codex"


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

# Set by the Authentik outpost, the only path a browser has to the app; the other path in, the API
# server's service proxy, carries no user (cluster/k8s/agentplane-staging/app/networkpolicy.yaml).
OPERATOR_HEADER = "x-authentik-username"
_LABEL_VALUE = TypeAdapter(LabelValue)


def _operator(request: Request) -> str | None:
    return request.headers.get(OPERATOR_HEADER)


def _require_operator(operator: Annotated[str | None, Depends(_operator)]) -> str:
    """The operator an approval or grant is recorded as; a caller without one may not decide."""
    if operator is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"no operator identity: the {OPERATOR_HEADER} header is absent"
        )
    try:
        return _LABEL_VALUE.validate_python(operator)
    except ValidationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"operator {operator!r} is not a label value") from error


Operator = Annotated[str, Depends(_require_operator)]


@router.get("")
async def list_sandboxes(
    inventory: Inventory, include_archived: Annotated[bool, Query(description="Also list archived sandboxes.")] = False
) -> list[SandboxView]:
    return await inventory.list_sandboxes(include_archived=include_archived)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(
    inventory: Inventory, egress: Egress, spec: NewSandbox, operator: Annotated[str | None, Depends(_operator)]
) -> SandboxView:
    """Create the Sandbox; picked policies become one binding it owns, granted by the operator."""
    by = _require_operator(operator) if spec.policies else None
    view = await inventory.create(spec)
    if by is not None:
        await egress.grant(sandbox=view.name, sandbox_uid=view.uid, policies=spec.policies, by=by)
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
    await inventory.delete(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{name}/egress")
async def sandbox_egress(inventory: Inventory, egress: Egress, name: str) -> list[BindingView]:
    """What may leave the sandbox: the bindings naming it, with their policies as they resolve."""
    return await egress.bindings_for(name, await inventory.labels(name))


@router.get("/{name}/egress/decisions")
async def sandbox_egress_decisions(inventory: Inventory, decisions: Decisions, name: str) -> list[Decision]:
    """What recently left or was refused, from the proxy; 502 when the proxy cannot be asked."""
    await inventory.labels(name)  # 404 for a sandbox that does not exist
    return await decisions.recent(name)


egress_router = APIRouter(prefix="/egress", tags=["egress"])


@egress_router.get("/policies")
async def list_policies(egress: Egress) -> list[PolicyView]:
    """The namespace's policies: what the create form offers to pick from."""
    return await egress.list_policies()


@egress_router.post("/bindings/{name}/approve", status_code=status.HTTP_204_NO_CONTENT)
async def approve_binding(egress: Egress, operator: Operator, name: str) -> Response:
    await egress.approve(name, by=operator)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@egress_router.post("/bindings/{name}/deny", status_code=status.HTTP_204_NO_CONTENT)
async def deny_binding(egress: Egress, operator: Operator, name: str) -> Response:
    await egress.deny(name, by=operator)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@egress_router.delete("/bindings/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_binding(egress: Egress, name: str) -> Response:
    """Revoke a runtime binding by deleting it; a binding from git is refused with 409."""
    await egress.revoke(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


threads = APIRouter(prefix="/threads", tags=["threads"])


def _store(request: Request) -> TrajectoryStore:
    store = request.app.state.store
    if not isinstance(store, TrajectoryStore):
        raise TypeError(f"app.state.store is {type(store).__name__}, not TrajectoryStore")
    return store


Store = Annotated[TrajectoryStore, Depends(_store)]


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
) -> FastAPI:
    if set(catalog) != set(Provider) or not all(catalog.values()):
        raise ValueError(f"the model catalog needs a non-empty list for every provider: {catalog=}")
    app = FastAPI(title="Agentplane", version="0")
    app.state.inventory = inventory
    app.state.bridge = bridge
    app.state.store = store
    app.state.models = catalog
    app.state.egress = egress
    app.state.decisions = decisions
    app.include_router(router)
    app.include_router(models)
    app.include_router(runner_bridge.router)
    app.include_router(threads)
    app.include_router(egress_router)

    @app.exception_handler(ThreadNotFoundError)
    async def _thread_not_found(_request: Request, error: ThreadNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        # The Deployment's probe: the process serves; the inventory's own reachability is per request.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.exception_handler(SandboxNotFoundError)
    async def _not_found(_request: Request, error: SandboxNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(BindingNotFoundError)
    async def _binding_not_found(_request: Request, error: BindingNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(DecisionsUnavailableError)
    async def _decisions_unavailable(_request: Request, error: DecisionsUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(error)})

    @app.exception_handler(FluxOwnedBindingError)
    async def _flux_owned(_request: Request, error: FluxOwnedBindingError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

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

    @app.exception_handler(runner_bridge.SessionStreamingError)
    async def _streaming(_request: Request, error: runner_bridge.SessionStreamingError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.MalformedMessageError)
    async def _malformed(_request: Request, error: runner_bridge.MalformedMessageError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(error)})

    return app
