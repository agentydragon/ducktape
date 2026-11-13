"""Production service composition without the app or an in-test Kubernetes cluster."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.fixtures import config, endpoint, model, sandbox, spec, workspace
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess, pid1_container
from agentplane.history_service.testing.fixtures import service_database_template, service_history_db_url
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.net.peertls.config import MTLS
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
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
    "config",
    "control",
    "database_clones",
    "database_names",
    "dispatcher",
    "dispatcher_database_url",
    "endpoint",
    "gateway_service",
    "gateway_token",
    "history_service",
    "ingress",
    "kubeconfig",
    "model",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
    "runtime_binary",
    "runtime_client",
    "sandbox",
    "service_database_template",
    "service_history_db_url",
    "spec",
    "start_runtime",
    "workspace",
]

CLIENT = ServiceAccountRef(namespace="test-contract-client", name="client")
DISPATCHER_AUDIENCE = "test-contract-dispatcher"
GATEWAY_AUDIENCE = "test-contract-gateway"


@pytest.fixture
def runtime_binary() -> Path:
    """The production Runtime executable unless a test module overrides this fixture."""
    return Runfiles.CreateOrRaise().root() / launches.RUNTIME_BINARY


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
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=(sandbox.namespace,),
        svid_ca=gateway_service.environment_runtime_ca,
        policy_namespace=sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
async def ingress(history_service: GoHistoryService) -> AsyncIterator[Endpoint]:
    async with history_tunnel(history_service.ingress_target) as proxy:
        yield Endpoint(
            target=history_service.ingress_target,
            svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address=proxy),
        )


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


@pytest.fixture
async def start_runtime(
    config: RunnerTestConfig,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    gateway_service: GatewayProcess,
    client_token: Path,
    sandbox: SandboxCluster,
    runtime_binary: Path,
    tmp_path: Path,
) -> AsyncIterator[Callable[[], Awaitable[RunnerProcess]]]:
    environment = {**config.harness_environment, "ANTHROPIC_AUTH_TOKEN": launches.TOKEN, "OTEL_SDK_DISABLED": "true"}
    remote = gateway_service.gateway_client(client_token, request_timeout_s=2)
    async with model_config() as model_endpoint, AsyncExitStack() as processes:
        command = launches.command(
            config, control=control, ingress=ingress, model_config_target=model_endpoint, runner_binary=runtime_binary
        )

        async def stop(process: RunnerProcess) -> None:
            try:
                await process.stop()
            finally:
                await process.close()

        async def start() -> RunnerProcess:
            container = await asyncio.to_thread(pid1_container, command, tmp_path, environment)
            await asyncio.to_thread(container.__enter__)
            process = RunnerProcess(container)
            processes.push_async_callback(stop, process)
            await gateway_service.wait_for_runner(remote, sandbox.destination)
            return process

        yield start
