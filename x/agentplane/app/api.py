"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import grpc
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict

from x.agentplane.app import bridge as runner_bridge
from x.agentplane.app.inventory import NewSandbox, Provider, SandboxInventory, SandboxNotFoundError, SandboxView
from x.agentplane.app.trajectory import ThreadView, TrajectoryStore
from x.agentplane.runner.client import RunnerError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])

# The models each harness may be opened with, by provider: the app's configuration, offered to the
# session form. A thread carries its model; a sandbox is a Pod and carries none.
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


@router.get("")
async def list_sandboxes(
    inventory: Inventory, include_archived: Annotated[bool, Query(description="Also list archived sandboxes.")] = False
) -> list[SandboxView]:
    return await inventory.list_sandboxes(include_archived=include_archived)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(inventory: Inventory, spec: NewSandbox) -> SandboxView:
    return await inventory.create(spec)


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


threads = APIRouter(prefix="/threads", tags=["threads"])


def _store(request: Request) -> TrajectoryStore:
    store = request.app.state.store
    if not isinstance(store, TrajectoryStore):
        raise TypeError(f"app.state.store is {type(store).__name__}, not TrajectoryStore")
    return store


Store = Annotated[TrajectoryStore, Depends(_store)]


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


@threads.get("")
async def list_threads(store: Store) -> list[ThreadView]:
    """Every persisted thread, newest first; a thread outlives its sandbox."""
    return await store.list_threads()


@threads.get("/{thread_id}")
async def get_thread(store: Store, thread_id: UUID) -> ThreadView:
    view = await store.get_thread(thread_id)
    if view is None:
        raise ThreadNotFoundError(thread_id)
    return view


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
    inventory: SandboxInventory, bridge: runner_bridge.RunnerBridge, store: TrajectoryStore, catalog: ModelCatalog
) -> FastAPI:
    if set(catalog) != set(Provider) or not all(catalog.values()):
        raise ValueError(f"the model catalog needs a non-empty list for every provider: {catalog=}")
    app = FastAPI(title="Agentplane", version="0")
    app.state.inventory = inventory
    app.state.bridge = bridge
    app.state.store = store
    app.state.models = catalog
    app.include_router(router)
    app.include_router(models)
    app.include_router(runner_bridge.router)
    app.include_router(threads)

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
