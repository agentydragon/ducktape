"""REST surface over the sandbox inventory; the OpenAPI schema is FastAPI's from these signatures."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from x.agentplane.app import bridge as runner_bridge
from x.agentplane.app.inventory import (
    NewSandbox,
    SandboxInventory,
    SandboxNotFoundError,
    SandboxNotProvisionedError,
    SandboxView,
)
from x.agentplane.runner.client import RunnerError

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


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


def create_app(inventory: SandboxInventory, bridge: runner_bridge.RunnerBridge) -> FastAPI:
    app = FastAPI(title="Agentplane", version="0")
    app.state.inventory = inventory
    app.state.bridge = bridge
    app.include_router(router)
    app.include_router(runner_bridge.router)

    @app.exception_handler(SandboxNotFoundError)
    async def _not_found(_request: Request, error: SandboxNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(SandboxNotProvisionedError)
    async def _not_provisioned(_request: Request, error: SandboxNotProvisionedError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(runner_bridge.SandboxNotReachableError)
    async def _not_reachable(_request: Request, error: runner_bridge.SandboxNotReachableError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

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
