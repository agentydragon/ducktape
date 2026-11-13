"""Production service composition without the app or an in-test Kubernetes cluster."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing.fixtures import endpoint, model, sandbox, spec, workspace
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.net.peertls.config import MTLS
from agentplane.history_service.testing.fixtures import service_database_template, service_history_db_url
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
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
    "control",
    "database_clones",
    "database_names",
    "dispatcher",
    "dispatcher_database_url",
    "endpoint",
    "gateway_service",
    "history_service",
    "ingress",
    "kubeconfig",
    "model",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
    "sandbox",
    "service_database_template",
    "service_history_db_url",
    "spec",
    "workspace",
]

CLIENT = ServiceAccountRef(namespace="test-contract-client", name="client")
DISPATCHER_AUDIENCE = "test-contract-dispatcher"
GATEWAY_AUDIENCE = "test-contract-gateway"


@pytest.fixture
def dispatcher_database_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
async def gateway_service(
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, tmp_path: Path
) -> AsyncIterator[GatewayProcess]:
    workload = ServiceAccountRef(namespace=sandbox.namespace, name="gateway")
    token_file = tmp_path / "gateway-token"
    token_file.write_text(await api_server.workload_token(workload, [GATEWAY_AUDIENCE]))
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
        runner_proxy_token_file=token_file,
    ) as service:
        yield service


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
