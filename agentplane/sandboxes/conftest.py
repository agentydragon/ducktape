"""Shared in-memory Kubernetes fixtures for Sandbox readers and inventory tests."""

from collections.abc import AsyncIterator
from typing import cast

import pytest
from kubernetes_asyncio import client as k8s_client

from agentplane.sandboxes.egress_views import EgressReader
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from util.kubernetes import CustomObjectsClient


@pytest.fixture(autouse=True)
async def close_fake_clients() -> AsyncIterator[None]:
    yield
    await close_fake_api_clients()


@pytest.fixture
async def cluster() -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster() as started:
        yield started


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def egress(custom_objects: FakeCustomObjectsApi) -> EgressReader:
    return EgressReader(namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom_objects))


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom_objects),
        core_v1=cast(k8s_client.CoreV1Api, core_v1),
    )
