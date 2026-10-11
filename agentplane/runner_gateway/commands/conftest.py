"""Isolated, migrated PostgreSQL fixture for the command admission store."""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.runner_gateway.commands.database_migrate import RUNNER
from util.testing.postgres import PostgresDatabaseTemplate, database_name_for_test
from util.testing.postgres_fixtures import postgres_container

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def commands_database_template(postgres_container: PostgresContainer) -> Iterator[PostgresDatabaseTemplate]:
    """Apply the command schema once, then clone a writable database for each test."""
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    with PostgresDatabaseTemplate(admin, f"commands_template_{uuid4().hex}") as template:
        RUNNER.apply(template.url)
        yield template


@pytest.fixture
def db_url(commands_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest) -> Iterator[str]:
    name = database_name_for_test(request.node.nodeid)
    with commands_database_template.clone(name) as url:
        yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
