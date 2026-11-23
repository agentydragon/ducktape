"""Shared fixtures for browser tests backed by the live app and gateway."""

from collections.abc import AsyncIterator, Iterator

import pytest
from playwright.async_api import Page

from agentplane.app.testing.replication_source import SANDBOX
from agentplane.history_service.testing.electric_service import ElectricService
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from devinfra.testing.output_writer import BoundTestOutputs
from testing.frontend_visual.capture import VisualPage


@pytest.fixture
def review_view(page: Page, test_outputs: BoundTestOutputs) -> Iterator[VisualPage]:
    with VisualPage(page, outputs=test_outputs, title="Agentplane Thread browser") as view:
        yield view


@pytest.fixture
async def service_history_db_url(electric: ElectricService) -> str:
    return electric.database_url


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    """The `ReplicationSource`'s."""
    async with sandbox_cluster(api_server, name=SANDBOX) as started:
        yield started
