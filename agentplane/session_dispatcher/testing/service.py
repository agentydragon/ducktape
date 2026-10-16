"""Run the production Dispatcher against a test Kubernetes API and database."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from kubernetes_asyncio import client as k8s

from agentplane.grpc.calls import dial
from agentplane.proto import session_dispatcher_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher import main as dispatcher_main
from agentplane.session_dispatcher.settings import Settings

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


@asynccontextmanager
async def running_dispatcher(
    settings: Settings, cluster: SandboxCluster, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Dispatcher]:
    stopping = asyncio.Event()

    async def until_stopped() -> None:
        await stopping.wait()

    async def configuration(path: Path | None) -> k8s.Configuration:
        del path
        return cluster.api.configuration

    monkeypatch.setattr(dispatcher_main, "kubernetes_configuration", configuration)
    monkeypatch.setattr(dispatcher_main, "until_terminated", until_stopped)
    task = asyncio.create_task(dispatcher_main.serve(settings), name="dispatcher-contract-service")
    try:
        async with dial(f"127.0.0.1:{settings.port}") as channel:
            async with asyncio.timeout(5):
                await channel.channel_ready()
            stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
            yield stub
    finally:
        stopping.set()
        await task
