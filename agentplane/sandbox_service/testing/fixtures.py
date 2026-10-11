"""Shared database fixture for tests composing History, Commands, and Fold."""

from collections.abc import AsyncIterator

import pytest

from agentplane.sandbox_service.testing.history import initialize_service_database, service_database
from util.testing.postgres import PostgresDatabaseTemplate


@pytest.fixture(scope="session")
async def service_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(postgres_admin_url, "service_test_template") as template:
        async with service_database(template.url) as engine:
            await initialize_service_database(engine)
        yield template
