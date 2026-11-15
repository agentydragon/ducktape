"""Native EnvironmentService and Sandbox fixtures for the App-free scenario suites."""

from collections.abc import AsyncIterator

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients, native_platform_clients
from agentplane.system_tests.driver.native_pod_pool import Borrow, shared_pod
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.sandbox import Sandboxes

# gazelle:include_dep @pypi//protobuf


@pytest.fixture(scope="module")
async def native_platform(
    cluster: Cluster, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[NativePlatformClients]:
    async with native_platform_clients(cluster, tmp_path_factory.mktemp("native-platform")) as clients:
        yield clients


@pytest.fixture(scope="module")
async def native_trust_pod(cluster: Cluster, native_platform: NativePlatformClients) -> AsyncIterator[Borrow]:
    async with shared_pod(cluster, native_platform) as borrow:
        yield borrow


@pytest.fixture
async def native_sandbox(cluster: Cluster, native_platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
    async with native_sandbox_scope(cluster, native_platform) as create:
        yield create
