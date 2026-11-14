"""Real Gateway, envtest, Sandbox, and PostgreSQL fixtures for native contracts."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentplane.env.config import EnvironmentRuntimeControl
from agentplane.net.peertls.config import MTLS
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from agentplane.testing.sandbox_fixtures import sandbox
from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

__all__ = [
    "api_server",
    "client_token",
    "control",
    "database_clones",
    "database_names",
    "dispatcher",
    "dispatcher_database_url",
    "gateway_service",
    "gateway_token",
    "kubeconfig",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
    "runtime_client",
    "sandbox",
]

CLIENT = ServiceAccountRef(namespace="test-contract-client", name="client")
DISPATCHER_AUDIENCE = "test-contract-dispatcher"
GATEWAY_AUDIENCE = "test-contract-gateway"


@pytest.fixture
def dispatcher_database_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
async def gateway_token(api_server: KubernetesApiServer, sandbox: SandboxCluster, tmp_path: Path) -> Path:
    workload = ServiceAccountRef(namespace=sandbox.namespace, name="gateway")
    path = tmp_path / "gateway-token"
    path.write_text(await api_server.workload_token(workload, [GATEWAY_AUDIENCE]))
    return path


@pytest.fixture
async def gateway_service(
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, gateway_token: Path
) -> AsyncIterator[GatewayProcess]:
    workload = ServiceAccountRef(namespace=sandbox.namespace, name="gateway")
    async with run_gateway(
        api_server.kubeconfig,
        database_url=dispatcher_database_url,
        namespace=sandbox.namespace,
        dispatcher_callers=(CLIENT,),
        gateway_callers=(CLIENT, workload),
        workload=workload,
        environment_readers=(CLIENT,),
        environment_managers=(CLIENT,),
        operator_callers=(CLIENT,),
        dispatcher_audience=DISPATCHER_AUDIENCE,
        gateway_audience=GATEWAY_AUDIENCE,
        runner_proxy_token_file=gateway_token,
    ) as service:
        yield service


@pytest.fixture
def runtime_client(gateway_service: GatewayProcess, gateway_token: Path) -> SandboxControlClient:
    # Runtime mutation RPCs accept only the actual Gateway/Dispatcher service account.
    return gateway_service.gateway_client(gateway_token)


@pytest.fixture
async def client_token(api_server: KubernetesApiServer, tmp_path: Path) -> Path:
    path = tmp_path / "client-token"
    path.write_text(await api_server.workload_token(CLIENT, [DISPATCHER_AUDIENCE, GATEWAY_AUDIENCE]))
    return path


@pytest.fixture
async def dispatcher(gateway_service: GatewayProcess, client_token: Path) -> AsyncIterator[SessionDispatcherClient]:
    async with SessionDispatcherClient.connect(
        gateway_service.target,
        token_file=client_token,
        request_timeout_s=10,
        channel_credentials=gateway_service.channel_credentials(),
    ) as client:
        yield client


@pytest.fixture
def control(gateway_service: GatewayProcess, sandbox: SandboxCluster, tmp_path: Path) -> EnvironmentRuntimeControl:
    return EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=gateway_service.issue_environment_runtime_tls(
                sandbox.environment_runtime_identity_uri, tmp_path / "runtime-tls"
            )
        ),
    )
