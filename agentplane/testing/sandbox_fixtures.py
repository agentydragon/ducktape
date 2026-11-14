"""A real Sandbox API record for tests that own their Kubernetes control plane."""

from collections.abc import AsyncIterator

import pytest

from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as started:
        yield started
