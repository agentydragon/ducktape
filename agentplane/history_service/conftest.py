"""Isolated, migrated PostgreSQL fixtures for the history store."""

from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.history_service.storage.store import Store
from agentplane.testing.electric_role import ensure_electric_role
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def history_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """One migrated History Service database per test process, cloned for each case."""
    # The cluster-wide Electric login must exist before migration grants are copied to clones.
    ensure_electric_role(postgres_admin_url)
    with migrated_database_template(postgres_admin_url, "history_template", RUNNER) as template:
        yield template


@pytest.fixture
def db_url(history_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(history_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)
