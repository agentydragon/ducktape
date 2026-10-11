"""Real PostgreSQL, migrated by the same image-owned runner used in deployment."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.action_service.testing.fixtures import action_database_template, echo_catalog, echo_executor
from agentplane.history_service.testing.fixtures import service_database_template
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.store import Store
from agentplane.runner.testing.fixtures import RunnerHandle, config, endpoint, harness, model, runner, spec, workspace
from agentplane.testing.failure_journal import call_failed, capture_native_journal, pytest_runtest_makereport
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

# Both native harnesses, not an app-managed session or app database.
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg

logger = logging.getLogger(__name__)


@pytest.fixture
async def failed_native_journal(request: pytest.FixtureRequest, runner: RunnerHandle) -> AsyncIterator[None]:
    yield
    if not call_failed(request):
        return
    try:
        async with asyncio.timeout(3):
            await capture_native_journal(request, runner)
    except Exception as error:
        logger.warning("native journal capture failed: %s", type(error).__name__)


database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")


@pytest.fixture
def sandbox_service_db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def store(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
async def action_engine(
    action_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(
        create_async_engine(database_clones.clone(action_database_template, drivername="postgresql+asyncpg"))
    ) as engine:
        yield engine
