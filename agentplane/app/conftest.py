"""Real Go app, History, PostgreSQL, Kubernetes and runner fixtures."""

import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.app.testing.runner_gateway import (
    app_token_file,
    dispatcher_database_url,
    gateway_service,
    runner,
    sandbox,
    sandbox_control,
    session_dispatcher,
)
from agentplane.app.testing.thread_test_support import SPEC, ThreadHTTP, event_entry
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.fixtures import (
    history_on,
    history_service,
    service_database_template,
    service_history_db_url,
)
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.history_service.testing.history import History
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.bazel.runfiles import get_required_path
from util.net import refusing_tcp_endpoint
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//psycopg
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(scope="session")
def app_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    ensure_electric_role(postgres_admin_url)
    with PostgresDatabaseTemplate(postgres_admin_url, "agentplane_app_test_template") as template:
        migrated = subprocess.run(
            [get_required_path("_main/agentplane/app/go/agentplane-app_/agentplane-app"), "--migrate-only"],
            env={
                "AGENTPLANE_DATABASE_URL": make_url(template.url)
                .set(drivername="postgresql", query={"sslmode": "disable"})
                .render_as_string(hide_password=False)
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        yield template


@pytest.fixture
def db_url(app_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(app_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(create_async_engine(db_url)) as engine:
        yield engine


@pytest.fixture
def history_writer_namespaces(sandbox: SandboxCluster, namespace: SandboxNamespace) -> tuple[str, ...]:
    return (sandbox.namespace, namespace.name, WRITER.namespace)


@pytest.fixture
async def history(history_service: GoHistoryService) -> AsyncIterator[History]:
    async with History(history_service) as history:
        yield history


@pytest.fixture
def history_client(history_service: GoHistoryService) -> HistoryServiceClient:
    return history_service.reader


@pytest.fixture
def event_logs(history: History, namespace: SandboxNamespace, sandbox: SandboxCluster) -> SessionHistory:
    return SessionHistory(history, sandbox_namespaces={sandbox.name: sandbox.namespace, "live": namespace.name})


@pytest.fixture
def content(history: History) -> SessionHistory:
    return SessionHistory(history)


@pytest.fixture
async def namespace(api_server: KubernetesApiServer) -> SandboxNamespace:
    return await SandboxNamespace.new(api_server)


AUDIENCE = "agentplane-test"
_AGENT = "test-agent"
_STRANGER = "test-stranger"


async def _bearer(api_server: KubernetesApiServer, account: ServiceAccountRef) -> dict[str, str]:
    return {"Authorization": f"Bearer {await api_server.workload_token(account, [AUDIENCE])}"}


@pytest.fixture
def agent(namespace: SandboxNamespace) -> str:
    return f"system:serviceaccount:{namespace.name}:{_AGENT}"


@pytest.fixture
async def agent_auth(api_server: KubernetesApiServer, namespace: SandboxNamespace) -> dict[str, str]:
    return await _bearer(api_server, namespace.account(_AGENT))


@pytest.fixture
async def stranger_auth(api_server: KubernetesApiServer, namespace: SandboxNamespace) -> dict[str, str]:
    return await _bearer(api_server, namespace.account(_STRANGER))


@pytest.fixture
def app_config() -> dict[str, object]:
    return {}


@pytest.fixture
async def app(
    db_url: str,
    history_client: HistoryServiceClient,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    app_token_file: Path,
    agent: str,
    app_config: dict[str, object],
) -> AsyncIterator[AppProcess]:
    async with app_process(
        db_url,
        history_client,
        sandboxes=SandboxesAccess(kubeconfig=sandbox.server.kubeconfig, namespace=sandbox.namespace),
        gateway=GatewayAccess(gateway_service.target, app_token_file),
        config={"token_audience": AUDIENCE, "token_subjects": [agent]} | app_config,
    ) as process:
        yield process


@pytest.fixture
async def http(app: AppProcess) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=app.url, cookies=app.cookies, headers={"Origin": app.url}, timeout=60
    ) as client:
        yield client


@pytest.fixture
def store(http: httpx.AsyncClient) -> ThreadHTTP:
    return ThreadHTTP(http)


@pytest.fixture
def refused_address() -> Iterator[str]:
    with refusing_tcp_endpoint() as (host, port):
        yield f"{host}:{port}"
