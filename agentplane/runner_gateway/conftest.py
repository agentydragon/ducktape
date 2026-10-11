"""The Sandbox Service's fake cluster, whose Sandboxes the gateway resolves runners in, the gateway's
migrated commands database, and native runner fixtures."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner.testing.fixtures import client, config, endpoint, harness, model, runner, spec, workspace
from agentplane.runner_gateway.commands.conftest import database_template, db_url, engine
from agentplane.runner_gateway.commands.store import SubmissionStore
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.sandbox_service.conftest import close_fake_clients, cluster, token_file
from agentplane.sandbox_service.testing.kubernetes import Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
def sessions(engine: AsyncEngine) -> SessionRegistry:
    return SessionRegistry(engine)


@pytest.fixture
def submissions(engine: AsyncEngine) -> SubmissionStore:
    return SubmissionStore(engine)


@pytest.fixture
async def leases(cluster: Cluster) -> AsyncIterator[ConnectionLeases]:
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
