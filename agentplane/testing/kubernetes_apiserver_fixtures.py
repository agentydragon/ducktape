"""One envtest control plane per test target, and a client of it per test.

A conftest imports both fixtures by name. The control plane is session-scoped and loop-free; each
test's client is bound to that test's loop.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, control_plane, kubernetes_api


@pytest.fixture(scope="session")
def kubeconfig() -> Iterator[Path]:
    with control_plane() as path:
        yield path


@pytest.fixture
async def api_server(kubeconfig: Path) -> AsyncIterator[KubernetesApiServer]:
    async with kubernetes_api(kubeconfig) as server:
        yield server
