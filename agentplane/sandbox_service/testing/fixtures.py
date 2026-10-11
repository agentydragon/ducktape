"""Shared database fixture for tests composing History, Commands, and Fold."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.sandbox_service.testing.history import initialize_service_database
from util.testing.postgres import PostgresDatabaseTemplate
from util.testing.sqlalchemy_engines import managed_async_engine

# SQLAlchemy loads the asyncpg dialect at runtime.
# gazelle:include_dep @pypi//asyncpg


@pytest.fixture(scope="session")
async def service_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(postgres_admin_url, "service_test_template") as template:
        async with managed_async_engine(
            create_async_engine(make_url(template.url).set(drivername="postgresql+asyncpg"))
        ) as engine:
            await initialize_service_database(engine)
        yield template
