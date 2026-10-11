"""Shared pytest fixtures for the Study Casino test suite."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from testcontainers.postgres import PostgresContainer

from study_casino.store import _run_alembic_migrations
from util.testing.postgres import (
    PostgresDatabaseTemplate,
    create_database_sync,
    database_name_for_test,
    force_drop_database_sync,
)
from util.testing.postgres_fixtures import postgres_container


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    # gazelle:include_dep @pypi//psycopg
    # (SQLAlchemy loads the psycopg dialect at runtime via this URL scheme;
    # nothing imports it, so gazelle cannot see the dependency.)
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture(scope="session")
def casino_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """Migrate one template; runtime stores retain their normal startup schema check on each clone."""
    with PostgresDatabaseTemplate(postgres_admin_url, f"casino_template_{uuid4().hex}") as template:
        engine = create_engine(template.url)
        try:
            _run_alembic_migrations(engine)
        finally:
            engine.dispose()
        yield template


@pytest.fixture
def db_url(casino_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest) -> Iterator[str]:
    """A fresh writable clone for ordinary store and app tests."""
    with casino_database_template.clone(database_name_for_test(request.node.nodeid)) as url:
        yield url


@pytest.fixture
def empty_db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Generator[str]:
    """An empty database for the migration test that deliberately starts at revision 0004."""
    db_name = database_name_for_test(f"empty_{request.node.nodeid}")
    url = create_database_sync(postgres_admin_url, db_name)
    try:
        yield url
    finally:
        force_drop_database_sync(postgres_admin_url, db_name)
