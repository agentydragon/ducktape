"""Shared database fixture for tests composing History, Commands, and Fold."""

from collections.abc import AsyncIterator

import pytest

from agentplane.sandbox_service.testing.history import initialize_service_database
from util.testing.postgres import PostgresDatabaseTemplate
from util.testing.sqlalchemy_engines import postgres_async_engine


@pytest.fixture(scope="session")
async def service_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(postgres_admin_url, "service_test_template") as template:
        async with postgres_async_engine(template.url) as engine:
            await initialize_service_database(engine)
        yield template
