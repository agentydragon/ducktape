"""Process scaffolding for the agentplane gRPC service entry points: Kubernetes configuration, the
gRPC server's lifetime, background tasks and the health endpoint."""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None], *, name: str) -> AsyncIterator[None]:
    task = asyncio.create_task(coroutine, name=name)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@asynccontextmanager
async def serving(server: grpc.aio.Server, address: str, *, grace_s: float) -> AsyncIterator[int]:
    """Binds `address`, starts the server and yields the bound port, the ephemeral one for a `:0`
    address. On exit it stops the server, giving in-flight RPCs `grace_s` to finish."""
    port = server.add_insecure_port(address)
    await server.start()
    try:
        yield port
    finally:
        await server.stop(grace=grace_s)


async def serve_health(host: str, port: int) -> None:
    health = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @health.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    await uvicorn.Server(uvicorn.Config(health, host=host, port=port, access_log=False)).serve()
