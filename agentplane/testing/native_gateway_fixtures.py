"""Real Dispatcher, Environments, envtest, Sandbox and PostgreSQL fixtures for native contracts."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentplane.env.config import EnvironmentRuntimeControl
from agentplane.net.peertls.config import MTLS, SANDBOX_CONTROLLER_URI
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from agentplane.testing.sandbox_fixtures import sandbox
from testing.postgres import DatabaseNames, create_temporary_database
from testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

__all__ = [
    "api_server",
    "control",
    "controller_enabled",
    "database_clones",
    "database_names",
    "dispatcher",
    "dispatcher_database_url",
    "gateway_service",
    "kubeconfig",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
    "runtime_client",
    "sandbox",
]

DISPATCHER_AUDIENCE = "test-contract-dispatcher"
GATEWAY_AUDIENCE = "test-contract-gateway"


@pytest.fixture
def dispatcher_database_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
def controller_enabled() -> bool:
    """No Runtime lifecycle caller is authorized unless a contract opts in."""
    return False


@pytest.fixture
async def gateway_service(
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, controller_enabled: bool
) -> AsyncIterator[GatewayProcess]:
    async with run_gateway(
        api_server.kubeconfig,
        database_url=dispatcher_database_url,
        namespace=sandbox.namespace,
        runtime_proxy_controller_callers=(SANDBOX_CONTROLLER_URI,) if controller_enabled else (),
        dispatcher_audience=DISPATCHER_AUDIENCE,
        gateway_audience=GATEWAY_AUDIENCE,
    ) as service:
        yield service


@pytest.fixture
def runtime_client(gateway_service: GatewayProcess) -> SandboxControlClient:
    return gateway_service.environment_runtime_client()


@pytest.fixture
async def dispatcher(gateway_service: GatewayProcess) -> AsyncIterator[SessionDispatcherClient]:
    async with SessionDispatcherClient.connect(
        gateway_service.target,
        token_file=None,
        request_timeout_s=10,
        channel_credentials=gateway_service.service_credentials("spiffe://agentplane/app"),
    ) as client:
        yield client


@pytest.fixture
def control(gateway_service: GatewayProcess, sandbox: SandboxCluster, tmp_path: Path) -> EnvironmentRuntimeControl:
    return EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=gateway_service.environment_service.issue_workload_tls(
                sandbox.environment_runtime_identity_uri, tmp_path / "runtime-tls"
            )
        ),
    )
