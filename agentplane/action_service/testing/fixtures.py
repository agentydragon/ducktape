"""Fixtures shared by Action Service schema and policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.database_migrate import RUNNER
from agentplane.action_service.db import make_engine
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
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
