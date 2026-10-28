"""Register the shared runner fixtures for runner tests."""

from agentplane.history_service.testing.fixtures import (
    history_service,
    service_database_template,
    service_history_db_url,
)
from agentplane.runner.testing.codex_exec_server_container import codex_exec_server_container
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import (
    config,
    driver,
    endpoint,
    gateway,
    harness,
    model,
    runner,
    runner_gateway,
    spec,
    workspace,
)
from agentplane.runner.testing.process_fixtures import start_runner
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
