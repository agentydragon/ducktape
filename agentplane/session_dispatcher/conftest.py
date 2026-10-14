"""Real PostgreSQL migrated by the Dispatcher's own migration runner, the fake API server and a scripted runner."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import grpc
import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.background_task import background
from agentplane.grpc_serving import serving
from agentplane.proto import runner_control_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.session_dispatcher.database_migrate import RUNNER
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.session_dispatcher.testing.scripted_runner import ScriptedRunner
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.kubernetes import CustomObjectsClient
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//psycopg

database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[DispatchStore]:
    store = DispatchStore(engine, queue_cap=100)
    async with store.wakeups.listener.listen():
        yield store


@pytest.fixture
async def cluster() -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster() as cluster:
        yield cluster


@pytest.fixture
async def runner() -> AsyncIterator[tuple[ScriptedRunner, str]]:
    scripted = ScriptedRunner()
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerServicer_to_server(scripted, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield scripted, f"127.0.0.1:{port}"


@pytest.fixture
async def sandboxes(cluster: SandboxCluster) -> AsyncIterator[SandboxWatch]:
    watch = SandboxWatch(
        cast(CustomObjectsClient, k8s_client.CustomObjectsApi(cluster.api)),
        namespace=SANDBOX_NAMESPACE,
        resync_seconds=60,
    )
    async with background(watch.run(), name="sandbox-watch"):
        await watch.wait_synced()
        yield watch


@pytest.fixture
def ended() -> EndedDispatches:
    return EndedDispatches()


@pytest.fixture
def runner_client(runner: tuple[ScriptedRunner, str], tmp_path: Path) -> RunnerClient:
    token_file = tmp_path / "token"
    token_file.write_text("test-only-token")
    return RunnerClient(
        control_target=runner[1], namespace=SANDBOX_NAMESPACE, token_file=token_file, request_timeout_s=5
    )
