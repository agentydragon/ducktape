"""Pytest configuration for mcp_infra tests."""

import pytest
from sqlalchemy.engine import make_url

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.mcp.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403
from util.testing.postgres_fixtures import postgres_admin_url, postgres_container


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture(scope="session")
def postgres_url(postgres_admin_url: str) -> str:
    # asyncpg takes a PostgreSQL DSN without SQLAlchemy's driver suffix.
    return make_url(postgres_admin_url).set(drivername="postgresql").render_as_string(hide_password=False)
