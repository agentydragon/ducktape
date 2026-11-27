"""Native Runner fixtures used by the Runner client acceptance tests."""

from agentplane.app.testing.gateway_fixtures import (
    app_token_file,
    dispatcher_database_url,
    gateway_service,
    history_writer_namespaces,
    sandbox,
    sandbox_control,
    session_dispatcher,
)
from agentplane.app.testing.runtime_fixtures import runner
from agentplane.env.e2e.testing.failure_journal import pytest_runtest_makereport
from agentplane.env.e2e.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.history_service.testing.fixtures import (
    history_service,
    service_database_template,
    service_history_db_url,
)
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
