"""Fake Kubernetes fixtures, a caller bearer file, and the service schema for integration tests."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
from agentplane.sandbox_service.testing.fixtures import service_database_template
from agentplane.sandbox_service.testing.kubernetes import Cluster, kubernetes
from util.kubernetes import CustomObjectsClient
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, postgres_container


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


@pytest.fixture
def db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
