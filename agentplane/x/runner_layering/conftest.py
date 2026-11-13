"""Register runner subprocess fixtures for the image-layering experiment."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentplane.env.e2e.testing.driver import RunnerDriver
from agentplane.env.e2e.testing.failure_journal import FailureEvidence, pytest_runtest_makereport
from agentplane.env.e2e.testing.fixtures import (
    endpoint,
    gateway,
    harness,
    history_service,
    history_writer_namespaces,
    model,
    runner_gateway,
    sandbox,
    spec,
    workspace,
)
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process_fixtures import start_runner
from agentplane.history_service.testing.fixtures import service_database_template, service_history_db_url
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture(autouse=True)
def retain_failed_journal(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[None]:
    yield
    if (evidence := FailureEvidence.for_failed_call(request)) is not None:
        evidence.capture_retained_journals(tmp_path / "state")


@pytest.fixture
async def driver(gateway: Gateway, history_service: GoHistoryService) -> AsyncIterator[RunnerDriver]:
    async with RunnerDriver(gateway, history_service.reader) as started:
        yield started
