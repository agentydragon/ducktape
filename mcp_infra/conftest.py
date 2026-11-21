"""Pytest fixtures for mcp_infra tests."""

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403
from testing.postgres_fixtures import postgres_admin_url, postgres_container, postgres_image
from util.db_url import render_postgresql_url


@pytest.fixture(scope="session")
def postgres_url(postgres_admin_url: str) -> str:
    # asyncpg takes a PostgreSQL DSN without SQLAlchemy's driver suffix.
    return render_postgresql_url(postgres_admin_url, "postgresql")
