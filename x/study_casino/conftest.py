"""Shared pytest fixtures for the Study Casino test suite.

Tests run against an ephemeral Postgres testcontainer (`postgres:18`,
preloaded from Bazel runfiles). Each test gets its own database carved
out of a session-scoped container so the per-test cost is just a
`CREATE DATABASE` + alembic upgrade.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from util.testing.postgres import force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    # gazelle:include_dep @pypi//psycopg
    # (SQLAlchemy loads the psycopg dialect at runtime via this URL scheme;
    # nothing imports it, so gazelle cannot see the dependency.)
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Generator[str]:
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_") or "casino_test"
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    yield postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    force_drop_database_sync(postgres_admin_url, db_name)
