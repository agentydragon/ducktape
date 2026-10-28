"""Native Runner fixtures used by the Runner client acceptance tests."""

import pytest

from agentplane.app.testing.runner_gateway import (
    app_token_file,
    dispatcher_database_url,
    gateway_service,
    history_writer_namespaces,
    runner,
    sandbox,
    sandbox_control,
    session_dispatcher,
)
from agentplane.history_service.testing.fixtures import (
    history_service,
    service_database_template,
    service_history_db_url,
)
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
def remote(sandbox_control: SandboxControlClient, runner: RunningRunner) -> SandboxControlClient:
    """The app-authenticated client routed through the real Go Runner Gateway."""
    return sandbox_control
