"""Real-Postgres fixtures for the standalone Action Service."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import pytest
import yaml
from fastmcp import FastMCP
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

from util.bazel.runfiles import get_required_path
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container
from x.agentplane.action_service.catalog import ActionCatalog, ActionDefinition, ActionGroup, McpExecutorBinding
from x.agentplane.action_service.database_migrate import apply_migrations
from x.agentplane.action_service.db import make_engine
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState

# SQLAlchemy loads these dialects from URLs; Gazelle cannot infer them.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture
def db_url(postgres_container: PostgresContainer, request: pytest.FixtureRequest) -> Iterator[str]:
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_")
    url = create_database_sync(admin_url, db_name)
    async_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    apply_migrations(async_url)
    yield async_url
    force_drop_database_sync(admin_url, db_name)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def echo_catalog() -> ActionCatalog:
    return ActionCatalog(
        groups={
            "agentplane": ActionGroup(
                title="Echo",
                description="Fixture-only echo action.",
                executor=McpExecutorBinding(description="Test-only catalog; executor explicitly injected"),
                actions={"echo": ActionDefinition(description="Echo arguments")},
            )
        }
    )


@pytest.fixture
def everything_url() -> Iterator[str]:
    deployment = yaml.safe_load(
        get_required_path("_main/cluster/k8s/agentplane-testing/actions/mcp-everything-deployment.yaml").read_text()
    )
    container_spec = deployment["spec"]["template"]["spec"]["containers"][0]
    with (
        DockerContainer(container_spec["image"])
        .with_kwargs(entrypoint=container_spec["command"], read_only=True, user="1000:1000")
        .with_command([])
        .with_exposed_ports(3001) as container
    ):
        wait_for_logs(container, "MCP Streamable HTTP Server listening on port", timeout=30, raise_on_exit=True)
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(3001)}/mcp"


class AlwaysLiveLease:
    renewal_interval = timedelta(seconds=1)

    async def heartbeat(self) -> bool:
        return True


@pytest.fixture
def execution_lease() -> ExecutionLease:
    return AlwaysLiveLease()


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


@pytest.fixture
def echo_executor() -> RecordingExecutor:
    return RecordingExecutor()


@pytest.fixture
async def mcp_executor(echo_catalog: ActionCatalog) -> AsyncIterator[McpActionGroupExecutor]:
    server = FastMCP("test-actions")

    @server.tool
    def echo(n: int) -> dict[str, int]:
        return {"n": n}

    executor = McpActionGroupExecutor("agentplane", echo_catalog.groups["agentplane"], server)
    await executor.start()
    try:
        yield executor
    finally:
        await executor.close()
