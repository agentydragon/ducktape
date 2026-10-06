"""Postgres testcontainer wiring for the budget read-model tests."""

from __future__ import annotations

import pytest
from testcontainers.postgres import PostgresContainer

from util.testing.postgres_fixtures import postgres_container


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"
