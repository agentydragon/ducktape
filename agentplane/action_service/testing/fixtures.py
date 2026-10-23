"""Reusable Action Service fixtures and helpers.

Keep shared fixtures and test helpers in a normal module so other packages can use them
without importing this package's conftest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionDefinition,
    ActionGroup,
    ActionIdentity,
    McpExecutorBinding,
)
from agentplane.action_service.database_migrate import RUNNER
from agentplane.action_service.db import make_engine, make_sessionmaker
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.models import ExecutionRequest, ExecutionResult, ExecutionState
from agentplane.action_service.service import ExecutionLease, Executor
from agentplane.action_service.store import ActionStore
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


@pytest.fixture
def store(engine: AsyncEngine) -> ActionStore:
    return ActionStore(make_sessionmaker(engine))


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
