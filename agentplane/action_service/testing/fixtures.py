"""Reusable Action Service fixtures and helpers.

Keep shared fixtures and test helpers in a normal module so other packages can use them
without importing this package's conftest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionDefinition,
    ActionGroup,
    ActionIdentity,
    McpExecutorBinding,
)
from agentplane.action_service.database_migrate import RUNNER
from agentplane.action_service.db import make_engine
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.mcp_executor import McpActionGroupExecutor
from agentplane.action_service.models import ExecutionRequest, ExecutionResult, ExecutionState
from agentplane.action_service.service import ExecutionLease, Executor
from agentplane.action_service.testing.credentials import OperatorIdp
from agentplane.action_service.testing.lifecycle import wait_available
from mcp_infra.testing.remote_server import as_remote_server
from util.net import bind_free_port
from util.testing.asgi import serve_app_sync
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from util.testing.postgres_fixtures import migrated_database_fixtures
from util.testing.sqlalchemy_engines import managed_async_engine

# SQLAlchemy loads these dialects from URLs; Gazelle cannot infer them.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


action_database_template, db_url = migrated_database_fixtures(
    RUNNER, drivername="postgresql+asyncpg", template_name="action_database_template"
)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(make_engine(db_url)) as engine:
        yield engine


@pytest.fixture(scope="session")
def operator_idp() -> Iterator[OperatorIdp]:
    private_key, public_key = generate_rsa_keypair()
    sock = bind_free_port()
    issuer = f"http://127.0.0.1:{sock.getsockname()[1]}"
    with serve_app_sync(
        build_mock_oidc_app(issuer_url=issuer, private_key=private_key, public_key=public_key), sock=sock
    ):
        yield OperatorIdp(issuer=issuer, signing_key=private_key)


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


class AlwaysLiveLease(ExecutionLease):
    """Always still owned, for driving an executor with no claimed Execution behind it."""

    renewal_interval = timedelta(seconds=1)

    def __init__(self) -> None:
        """No store or claim: `heartbeat` answers without them."""

    async def heartbeat(self) -> bool:
        return True


@pytest.fixture
def execution_lease() -> ExecutionLease:
    return AlwaysLiveLease()


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


@pytest.fixture
def echo_executor() -> RecordingExecutor:
    return RecordingExecutor()


class ScriptedExecutor(Executor):
    """Answers each Action with the result a test set for it, once `release` is set; it starts set,
    and a test clears it to hold an execution running."""

    def __init__(self) -> None:
        self.results: dict[ActionIdentity, ExecutionResult] = {}
        self.release = asyncio.Event()
        self.release.set()

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        await self.release.wait()
        return self.results[request.action]


@pytest.fixture
def scripted() -> ScriptedExecutor:
    return ScriptedExecutor()


@asynccontextmanager
async def lifespan_in_own_task(app: Starlette) -> AsyncIterator[None]:
    """Hold `app`'s lifespan open in a task of its own. pytest-asyncio resumes a yield fixture's
    teardown in another task, and the MCP lifespan's AnyIO scopes must enter and exit in the same
    one, as they do under uvicorn."""
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    stopping = asyncio.Event()

    async def lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set_result(None)
                await stopping.wait()
        except BaseException as error:
            if not started.done():
                started.set_exception(error)
            raise

    task = asyncio.create_task(lifespan())
    try:
        await started
        yield
    finally:
        stopping.set()
        await task


@pytest.fixture
async def mcp_executor(echo_catalog: ActionCatalog) -> AsyncIterator[McpActionGroupExecutor]:
    server = FastMCP("test-actions")

    @server.tool
    def echo(n: int) -> dict[str, int]:
        return {"n": n}

    executor = McpActionGroupExecutor("agentplane", echo_catalog.groups["agentplane"], server)
    async with executor:
        await wait_available(echo_catalog.groups["agentplane"])
        yield executor


@pytest.fixture
async def echo_mcp_url() -> AsyncIterator[str]:
    """A real streamable-HTTP MCP server for tests that need an actual network URL rather
    than the in-memory transport `mcp_executor` uses -- exercises the same wire path
    (`transport: streamable-http`) production Action Service settings use."""
    server = FastMCP("echo-fixture")

    @server.tool
    def echo(message: str) -> str:
        return f"Echo: {message}"

    async with as_remote_server(server) as remote:
        yield remote.url


@pytest.fixture
def github_visibility() -> Callable[..., RepositoryVisibilityService]:
    """Stands in for GitHub's unauthenticated repository endpoint: 200 for the `public` pairs named,
    404 for anything else, 500 for everything when `unavailable`."""

    def make(*public: tuple[str, str], unavailable: bool = False) -> RepositoryVisibilityService:
        confirmed = {(owner.casefold(), repository.casefold()) for owner, repository in public}

        def handle(request: httpx.Request) -> httpx.Response:
            if unavailable:
                return httpx.Response(500)
            _, _, owner, repository = request.url.path.split("/", 3)
            if (owner.casefold(), repository.casefold()) in confirmed:
                return httpx.Response(200, json={"private": False})
            return httpx.Response(404)

        return RepositoryVisibilityService(
            httpx.AsyncClient(base_url="https://github-api.test", transport=httpx.MockTransport(handle)),
            ttl_seconds=3600.0,
        )

    return make
