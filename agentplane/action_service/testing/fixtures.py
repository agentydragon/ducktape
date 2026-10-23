"""Fixtures shared by Action Service schema and policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.database_migrate import RUNNER
from agentplane.action_service.db import make_engine
from util.testing.postgres_fixtures import migrated_database_fixtures
from util.testing.sqlalchemy_engines import managed_async_engine

# SQLAlchemy loads these dialects from URLs; Gazelle cannot infer them.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


action_database_template, db_url = migrated_database_fixtures(
    RUNNER, drivername="postgresql+asyncpg", template_name="action_database_template"
)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(make_engine(db_url)) as engine:
        yield engine
