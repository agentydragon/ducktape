"""Fixtures for app tests against the Go Runner Gateway, as deployed: a Ready Sandbox on the target's
Kubernetes API server, the Gateway's Dispatcher and Runner proxy over it, and the native runner
connected through it as that Sandbox's runner."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.live import LiveIndex
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import bearer, dial
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.runner.configuration import Endpoint, ReverseControl
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.failure_journal import FailureEvidence
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep @pypi//protobuf

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


async def discover(live_index: LiveIndex, server: KubernetesApiServer, *, namespace: str, name: str) -> None:
    """Index the Sandbox `name` and its Pod as the app's informers would."""
    live_index.sandboxes[name] = await k8s_client.CustomObjectsApi(server.api).get_namespaced_custom_object(
        GROUP, VERSION, namespace, SANDBOXES_PLURAL, name
    )
    live_index.pods[name] = await k8s_client.CoreV1Api(server.api).read_namespaced_pod(name, namespace)


@pytest.fixture
def dispatcher_database_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
async def gateway_service(
    api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, tmp_path: Path
) -> AsyncIterator[GatewayProcess]:
    token_file = tmp_path / "runner-gateway-token"
    await asyncio.to_thread(token_file.write_text, await api_server.workload_token(GATEWAY, [GATEWAY_AUDIENCE]))
    async with run_gateway(
        api_server.kubeconfig,
        database_url=dispatcher_database_url,
        namespace=sandbox.namespace,
        dispatcher_callers=(APP,),
        gateway_callers=(APP, GATEWAY),
        dispatcher_audience=DISPATCHER_AUDIENCE,
        gateway_audience=GATEWAY_AUDIENCE,
        runner_proxy_token_file=token_file,
        runner_transports=("reverse",),
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
    async with dial(gateway.target) as channel:
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
    if gateway_service.runner_target is None:
        raise AssertionError("reverse Runner transport did not open its private listener")
    control = ReverseControl(
        mode="reverse",
        target=gateway_service.runner_target,
        tls=gateway_service.issue_runner_tls(sandbox.runner_identity_uri, tmp_path / "runner-transport"),
    )
    ingress = Endpoint(target=history_service.ingress_target, bearer=await history_service.writer_token(sandbox.owner))
    launched = replace(config, launch=replace(config.launch, sandbox=sandbox.identity))
    async with RunningRunner(
        launched, runner_gateway, history=history_service.reader, control_endpoint=control, ingress_endpoint=ingress
    ) as started:
        await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)


async def delivered_opening(dispatcher: SessionDispatcherClient, session_id: UUID) -> None:
    """Return once the Dispatcher has delivered the Session's opening to its runner."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            response = await dispatcher.unary(
                dispatcher.stub.GetSessionOpening,
                session_dispatcher_pb2.GetSessionOpeningRequest(session_id=str(session_id)),
            )
            assert response.opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, response.opening


async def admitted(
    dispatcher: SessionDispatcherClient, dispatch: session_dispatcher_pb2.Dispatch
) -> session_dispatcher_pb2.Dispatch:
    """`dispatch` once its runner admitted it, as the Dispatcher's watch reports it."""
    stream = dispatcher.stub.WatchDispatches(
        session_dispatcher_pb2.WatchDispatchesRequest(
            filter=session_dispatcher_pb2.DispatchFilter(
                session_id=dispatch.session_id, command_ids=[dispatch.command_id]
            )
        ),
        metadata=await bearer(dispatcher.token_file),
        timeout=30,
    )
    try:
        async for response in stream:
            if not response.HasField("dispatch"):
                continue
            row: session_dispatcher_pb2.Dispatch = response.dispatch
            assert row.state not in {
                session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
            }, row
            if row.state in {
                session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
                session_dispatcher_pb2.DELIVERY_STATE_HARNESS_CONFIRMED,
            }:
                return row
    finally:
        stream.cancel()
    raise AssertionError("the Dispatch watch ended before the runner admitted it")
