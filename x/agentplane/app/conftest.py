"""Fixtures over the fake Kubernetes in `testing/kubernetes.py`."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container
from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.app.testing.kubernetes import NAMESPACE, TEMPLATE, FakeCoreV1Api, FakeCustomObjectsApi
from x.agentplane.app.trajectory import TrajectoryStore

# The per-test database is created over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg
# The bridge tests run one script against a local runner over both harnesses; those fixtures live
# with the runner.
from x.agentplane.runner.conftest import config, model, provider, runner, spec, upstream, workspace


@pytest.fixture
def db_url(postgres_container: PostgresContainer, request: pytest.FixtureRequest) -> Iterator[str]:
    """A pristine per-test database on the shared container, as an asyncpg URL."""
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_")
    url = create_database_sync(admin_url, db_name)
    yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    force_drop_database_sync(admin_url, db_name)


@pytest.fixture
async def store(db_url: str) -> AsyncIterator[TrajectoryStore]:
    store = TrajectoryStore.connect(db_url)
    await store.ensure_schema()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def bridge(store: TrajectoryStore) -> RunnerBridge:
    """A bridge with nothing to dial, for the inventory and thread routes."""

    async def unreachable(name: str) -> str:
        raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)

    return RunnerBridge(address_of=unreachable, store=store)


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(
        namespace=NAMESPACE, template=TEMPLATE, custom_objects=cast(Any, custom_objects), core_v1=cast(Any, core_v1)
    )
