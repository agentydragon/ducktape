"""Register the shared runner fixtures for runner tests."""

from agentplane.env.e2e.testing.failure_journal import pytest_runtest_makereport
from agentplane.env.e2e.testing.fixtures import (
    config,
    driver,
    endpoint,
    gateway,
    harness,
    history_service,
    history_writer_namespaces,
    model,
    runner,
    runner_gateway,
    sandbox,
    spec,
    workspace,
)
from agentplane.env.e2e.testing.process_fixtures import start_runner
from agentplane.history_service.testing.fixtures import service_database_template, service_history_db_url
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
