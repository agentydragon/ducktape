"""Shared database fixture for tests composing History and Fold."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.fold.models import Base as FoldBase
from agentplane.history_service.storage.db import Base
from util.testing.postgres import PostgresDatabaseTemplate
from util.testing.sqlalchemy_engines import postgres_async_engine


async def initialize_service_database(engine: AsyncEngine) -> None:
    """Create History and Fold schemas on an empty template database."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(FoldBase.metadata.create_all)


@pytest.fixture(scope="session")
async def service_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(postgres_admin_url, "service_test_template") as template:
        async with postgres_async_engine(template.url) as engine:
            await initialize_service_database(engine)
        yield template
