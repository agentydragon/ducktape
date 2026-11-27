"""Register the real-browser and app integration fixtures every browser suite here uses.

`sandbox` and `service_history_db_url` come from `thread_browser_backends`, not the integration
plugin, so the browser suites' Sandbox is the `ReplicationSource`'s and History reads Electric's
database."""

from agentplane.app.testing.gateway_fixtures import (
    dispatcher_database_url,
    gateway_service,
    sandbox_control,
    session_dispatcher,
)
from agentplane.app.testing.integration_fixtures import (
    app,
    app_config,
    app_database_template,
    content,
    db_url,
    engine,
    event_logs,
    gateway_access,
    history,
    history_writer_namespaces,
    http,
    kubernetes_auth,
    namespace,
    operator_auth,
    refused_address,
    store,
)
from agentplane.app.testing.thread_browser import (
    certificate,
    electric,
    page,
    replay_after,
    thread_browser,
    thread_browser_postgres,
    thread_browser_session,
    thread_source,
    viewport,
)
from agentplane.app.testing.thread_browser_backends import review_view, sandbox, service_history_db_url
from agentplane.history_service.testing.fixtures import history_on, history_service, service_database_template
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
