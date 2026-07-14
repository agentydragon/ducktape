"""Persistence configs map to the intended py-key-value backend, including a real Postgres
round-trip and the shared-store builder's non-optional contract."""

from __future__ import annotations

from collections.abc import Generator

import pytest
import pytest_bazel
from key_value.aio.stores.postgresql import PostgreSQLStore
from key_value.aio.stores.valkey import ValkeyStore
from testcontainers.postgres import PostgresContainer

from mcp_infra.persistence import (
    FilePersistence,
    PostgresPersistence,
    ValkeyPersistence,
    build_client_storage,
    build_shared_client_storage,
)
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image


@pytest.fixture(scope="session", autouse=True)
def _preload_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        # asyncpg DSN — plain postgresql://, NOT the SQLAlchemy postgresql+psycopg:// form.
        yield f"postgresql://postgres:postgres@{host}:{port}/postgres"
    finally:
        container.stop()


def test_build_client_storage_variants() -> None:
    assert build_client_storage(FilePersistence()) is None
    assert isinstance(
        build_shared_client_storage(ValkeyPersistence(kind="valkey", host="valkey.example.com")), ValkeyStore
    )


async def test_postgres_store_round_trip(postgres_url: str) -> None:
    store = build_shared_client_storage(
        PostgresPersistence(kind="postgres", url=postgres_url, table_name="mcp_oauth_kv")
    )
    assert isinstance(store, PostgreSQLStore)
    await store.setup()  # auto_create=True -> creates the table on first setup

    await store.put("agent-1", {"client_secret": "s"}, collection="clients")
    assert await store.get("agent-1", collection="clients") == {"client_secret": "s"}
    # Collections are isolated namespaces (the OIDCProxy uses several).
    assert await store.get("agent-1", collection="tokens") is None

    await store.delete("agent-1", collection="clients")
    assert await store.get("agent-1", collection="clients") is None

    await store.close()


if __name__ == "__main__":
    pytest_bazel.main()
