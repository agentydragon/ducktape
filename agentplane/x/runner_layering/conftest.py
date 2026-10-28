"""Register runner subprocess fixtures for the image-layering experiment."""

from collections.abc import AsyncIterator

import pytest

from agentplane.history_service.testing.fixtures import (
    history_service,
    history_writer_namespaces,
    service_database_template,
    service_history_db_url,
)
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.fixtures import endpoint, gateway, harness, model, runner_gateway, spec, workspace
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import start_runner
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
async def driver(gateway: Gateway, history_service: GoHistoryService) -> AsyncIterator[RunnerDriver]:
    async with RunnerDriver(gateway, history_service.reader) as started:
        yield started
