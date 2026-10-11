"""Native runner and disposable History + Commands database fixtures."""

from collections.abc import AsyncIterator, Iterator
from typing import cast
from uuid import uuid4

import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.runner.testing.fixtures import client, config, endpoint, harness, model, runner, spec, workspace
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.sandbox_service.testing.history import initialize_service_database, service_database
from agentplane.sandbox_service.testing.kubernetes import Cluster, kubernetes
from util.kubernetes import CustomObjectsClient
from util.testing.postgres import PostgresDatabaseTemplate, database_name_for_test
from util.testing.postgres_fixtures import postgres_container


@pytest.fixture(autouse=True)
async def close_fake_clients() -> AsyncIterator[None]:
    yield
    await close_fake_api_clients()


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    async with kubernetes() as started:
        yield started


@pytest.fixture(scope="session")
def service_postgres_admin_url(postgres_container: PostgresContainer) -> str:
    return (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )


@pytest.fixture(scope="session")
async def service_database_template(service_postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    """Create both service schemas once; each test gets a writable clone."""
    with PostgresDatabaseTemplate(service_postgres_admin_url, f"sandbox_service_template_{uuid4().hex}") as template:
        async with service_database(template.url) as engine:
            await initialize_service_database(engine)
        yield template


@pytest.fixture
def db_url(service_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest) -> Iterator[str]:
    with service_database_template.clone(database_name_for_test(request.node.nodeid)) as url:
        yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def egress(custom_objects: FakeCustomObjectsApi) -> EgressInventory:
    return EgressInventory(namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom_objects))


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom_objects),
        core_v1=cast(k8s_client.CoreV1Api, core_v1),
    )
