"""Fixtures for app tests against the Go Runner Gateway, as deployed: a Ready Sandbox on the target's
Kubernetes API server, the Gateway's Dispatcher and Runner proxy over it, and the native runner
connected through it as that Sandbox's runner."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.failure_journal import FailureEvidence
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.environment_runtime_transport.config import MTLS
from agentplane.grpc.calls import dial
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService, go_history_service
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


@asynccontextmanager
async def _history_tunnel(target: str) -> AsyncIterator[str]:
    """Forward CONNECT bytes to this fixture's History listener; TLS stays end to end."""
    tasks: set[asyncio.Task[None]] = set()
    host, port = target.rsplit(":", 1)

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(10):
                headers = await reader.readuntil(b"\r\n\r\n")
            if headers.split(b"\r\n", 1)[0] != f"CONNECT {target} HTTP/1.1".encode():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            remote, upstream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            forward = asyncio.create_task(pump(reader, upstream))
            backward = asyncio.create_task(pump(remote, writer))
            try:
                await asyncio.wait((forward, backward), return_when=asyncio.FIRST_COMPLETED)
            finally:
                forward.cancel()
                backward.cancel()
                await asyncio.gather(forward, backward, return_exceptions=True)
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                with suppress(ConnectionError):
                    await upstream.wait_closed()
            with suppress(ConnectionError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


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


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest,
    config: RunnerTestConfig,
    runner_gateway: Gateway,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    tmp_path: Path,
) -> AsyncIterator[RunningRunner]:
    """The production Go runner as `sandbox`'s, taking control through the Go Runner Gateway; it still
    pushes its journal directly to the Go History Service ingress. Failed tests retain native
    evidence."""
    control = EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=gateway_service.issue_environment_runtime_tls(
                sandbox.environment_runtime_identity_uri, tmp_path / "environment-runtime-transport"
            )
        ),
    )
    launched = replace(config, launch=replace(config.launch, sandbox=sandbox.identity))
    # Scripted writers and component readers keep the ordinary History replica. The runner's
    # replica authenticates its actual Sandbox Identity certificate on the production SVID path.
    ingress_directory = tmp_path / "runner-history"
    ingress_directory.mkdir()
    async with (
        go_history_service(
            history_service.database_url,
            api=history_service.api,
            directory=ingress_directory,
            writer_namespaces=(sandbox.namespace,),
            svid_ca=gateway_service.environment_runtime_ca,
            policy_namespace=sandbox.namespace,
        ) as ingress_service,
        _history_tunnel(ingress_service.ingress_target) as proxy,
        RunningRunner(
            launched,
            runner_gateway,
            history=history_service.reader,
            control_endpoint=control,
            ingress_endpoint=Endpoint(
                target=ingress_service.ingress_target,
                svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address=proxy),
            ),
            observe_control=False,
        ) as started,
    ):
        await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)
