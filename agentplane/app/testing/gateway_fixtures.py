"""App-facing fixtures for a real Kubernetes API server and Runner Gateway."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agentplane.grpc.calls import dial
from agentplane.history_service.testing.go_service import WRITER
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from util.testing.postgres import DatabaseNames, create_temporary_database

DISPATCHER_AUDIENCE = "test-dispatcher"
GATEWAY_AUDIENCE = "test-gateway"
# The app calls the Dispatcher and the Runner proxy; the Gateway's own Dispatcher calls the proxy too.
APP = ServiceAccountRef(namespace="test-app-namespace", name="test-app")
GATEWAY = ServiceAccountRef(namespace="test-gateway-namespace", name="test-runner-gateway")


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
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, tmp_path: Path
) -> AsyncIterator[GatewayProcess]:
    workload = ServiceAccountRef(namespace=sandbox.namespace, name=GATEWAY.name)
    token_file = tmp_path / "runner-gateway-token"
    await asyncio.to_thread(token_file.write_text, await api_server.workload_token(workload, [GATEWAY_AUDIENCE]))
    async with run_gateway(
        api_server.kubeconfig,
        database_url=dispatcher_database_url,
        namespace=sandbox.namespace,
        dispatcher_callers=(APP,),
        gateway_callers=(APP, workload),
        workload=workload,
        environment_readers=(APP,),
        environment_managers=(APP,),
        operator_callers=(APP,),
        dispatcher_audience=DISPATCHER_AUDIENCE,
        gateway_audience=GATEWAY_AUDIENCE,
        runner_proxy_token_file=token_file,
    ) as service:
        yield service


@pytest.fixture
async def app_token_file(api_server: KubernetesApiServer, tmp_path: Path) -> Path:
    """The app's projected token, for both the Dispatcher and the Runner proxy."""
    token_file = tmp_path / "app-runner-gateway-token"
    await asyncio.to_thread(
        token_file.write_text, await api_server.workload_token(APP, [DISPATCHER_AUDIENCE, GATEWAY_AUDIENCE])
    )
    return token_file


@pytest.fixture
def sandbox_control(gateway_service: GatewayProcess, app_token_file: Path) -> SandboxControlClient:
    return gateway_service.gateway_client(app_token_file)


@asynccontextmanager
async def session_dispatcher_client(
    gateway: GatewayProcess, token_file: Path
) -> AsyncIterator[SessionDispatcherClient]:
    """A replica's own Dispatcher client: a channel of its own to the one Gateway."""
    async with dial(gateway.target, channel_credentials=gateway.channel_credentials()) as channel:
        yield SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=10)


@pytest.fixture
async def session_dispatcher(
    gateway_service: GatewayProcess, app_token_file: Path
) -> AsyncIterator[SessionDispatcherClient]:
    async with session_dispatcher_client(gateway_service, app_token_file) as client:
        yield client
