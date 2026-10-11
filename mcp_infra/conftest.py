"""Pytest configuration for mcp_infra tests."""

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.mcp.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403
from util.db_url import render_postgresql_url
from util.testing.postgres_fixtures import postgres_admin_url, postgres_container, postgres_image


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture(scope="session")
def postgres_url(postgres_admin_url: str) -> str:
    # asyncpg takes a PostgreSQL DSN without SQLAlchemy's driver suffix.
    return render_postgresql_url(postgres_admin_url, "postgresql")
