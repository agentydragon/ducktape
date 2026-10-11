"""Fake Kubernetes fixtures, a caller bearer file, and the service schema for integration tests."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
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


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "caller-token"
    path.write_text(CALLER_TOKEN)
    return path


@pytest.fixture(scope="session")
async def service_database_template(postgres_container: PostgresContainer) -> AsyncIterator[PostgresDatabaseTemplate]:
    """Initialize the service schemas once for the package's integration tests."""
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    with PostgresDatabaseTemplate(admin, f"sandbox_service_template_{uuid4().hex}") as template:
        async with service_database(template.url) as engine:
            await initialize_service_database(engine)
        yield template


@pytest.fixture
def db_url(service_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest) -> Iterator[str]:
    name = database_name_for_test(f"{request.node.nodeid}_service")
    with service_database_template.clone(name) as url:
        yield url


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with service_database(db_url) as database_engine:
        yield database_engine
