"""Pytest configuration for mcp_infra tests."""

import pytest
from testcontainers.postgres import PostgresContainer

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.mcp.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403
from util.testing.postgres_fixtures import postgres_container


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    # asyncpg DSN — plain postgresql://, NOT the SQLAlchemy postgresql+psycopg:// form.
    return f"postgresql://postgres:postgres@{host}:{port}/postgres"
