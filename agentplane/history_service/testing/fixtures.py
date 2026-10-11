"""Shared database fixture for tests composing History, Commands, and Fold.

The History and Commands production Alembic runners use the same default ``alembic_version`` table,
so a combined test database must not apply both runners. Build the test schema with SQLAlchemy
metadata once in a PostgreSQL template instead; the template does not create migration stamps.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.fold.models import Base as FoldBase
from agentplane.history_service.storage.db import Base
from agentplane.runner_gateway.commands import db as commands_db
from util.testing.postgres import PostgresDatabaseTemplate
from util.testing.sqlalchemy_engines import postgres_async_engine


async def initialize_service_database(engine: AsyncEngine) -> None:
    """Create History, Commands, and fold test schemas on an empty template database."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Production keeps admission in its own database; one test database serves both here.
        await connection.run_sync(commands_db.Base.metadata.create_all)
        await connection.run_sync(FoldBase.metadata.create_all)


@pytest.fixture(scope="session")
async def service_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(postgres_admin_url, "service_test_template") as template:
        async with postgres_async_engine(template.url) as engine:
            await initialize_service_database(engine)
        yield template
