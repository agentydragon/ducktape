"""App-facing fixtures for a real Kubernetes API server and Runner Gateway."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agentplane.grpc.calls import dial
from agentplane.history_service.testing.go_service import WRITER
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from testing.postgres import DatabaseNames, create_temporary_database

DISPATCHER_AUDIENCE = "test-dispatcher"
GATEWAY_AUDIENCE = "test-gateway"
# The app calls the Dispatcher and the Runner proxy.
APP = ServiceAccountRef(namespace="test-app-namespace", name="test-app")


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as started:
        yield started


@pytest.fixture
def history_writer_namespaces(sandbox: SandboxCluster) -> Sequence[str]:
    """The Sandbox's runner pushes its journal; scripted writers push as `WRITER`."""
    return (sandbox.namespace, WRITER.namespace)


@pytest.fixture
def dispatcher_database_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
async def gateway_service(
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str
) -> AsyncIterator[GatewayProcess]:
    async with run_gateway(
        api_server.kubeconfig, database_url=dispatcher_database_url, namespace=sandbox.namespace
    ) as service:
        yield service


@pytest.fixture
async def app_token_file(api_server: KubernetesApiServer, tmp_path: Path) -> Path:
    """A valid legacy App bearer used only to prove receiver rejection."""
    token_file = tmp_path / "app-runner-gateway-token"
    await asyncio.to_thread(
        token_file.write_text, await api_server.workload_token(APP, [DISPATCHER_AUDIENCE, GATEWAY_AUDIENCE])
    )
    return token_file


@pytest.fixture
def sandbox_control(gateway_service: GatewayProcess) -> SandboxControlClient:
    return gateway_service.environment_runtime_client()


@asynccontextmanager
async def session_dispatcher_client(gateway: GatewayProcess) -> AsyncIterator[SessionDispatcherClient]:
    """A replica's own Dispatcher client, connected to the Gateway listener."""
    async with dial(
        gateway.target, channel_credentials=gateway.service_credentials("spiffe://agentplane/app")
    ) as channel:
        yield SessionDispatcherClient(channel, request_timeout_s=10)


@pytest.fixture
async def session_dispatcher(gateway_service: GatewayProcess) -> AsyncIterator[SessionDispatcherClient]:
    async with session_dispatcher_client(gateway_service) as client:
        yield client
