"""Function-scoped fixtures for direct backend system-test callers."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients, native_platform_clients
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.sandbox import Sandboxes


@pytest.fixture
async def native_platform(cluster: Cluster) -> AsyncIterator[NativePlatformClients]:
    async with native_platform_clients(cluster) as clients:
        yield clients


@pytest.fixture
async def native_sandbox(cluster: Cluster, native_platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
    async with native_sandbox_scope(cluster, native_platform) as create:
        yield create
