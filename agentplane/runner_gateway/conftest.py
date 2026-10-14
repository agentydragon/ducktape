"""A fake Kubernetes cluster, the gateway's database, and native runner fixtures."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner, spec, workspace
from agentplane.runner_gateway.commands.conftest import database_template, db_url, engine
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.auth import CALLER_TOKEN
from agentplane.sandboxes.testing.fake_inventory import close_fake_api_clients
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture(autouse=True)
async def close_fake_clients() -> AsyncIterator[None]:
    yield
    await close_fake_api_clients()


@pytest.fixture
async def cluster() -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster() as started:
        yield started


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "caller-token"
    path.write_text(CALLER_TOKEN)
    return path


@pytest.fixture
def sessions(engine: AsyncEngine) -> SessionRegistry:
    return SessionRegistry(engine)


@pytest.fixture
async def leases(cluster: SandboxCluster) -> AsyncIterator[ConnectionLeases]:
    leases = ConnectionLeases()
    watch = asyncio.create_task(
        leases.watch(k8s_client.CoordinationV1Api(cluster.api), SANDBOX_NAMESPACE, resync_seconds=60).run()
    )
    try:
        yield leases
    finally:
        watch.cancel()
        with suppress(asyncio.CancelledError):
            await watch
