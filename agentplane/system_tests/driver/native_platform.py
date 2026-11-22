"""Native clients and exact control routing for system-test platform scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import grpc

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import identity_pb2
from agentplane.session_dispatcher.client import SandboxControlClient, SandboxRunner
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import forward
from agentplane.system_tests.driver.environment_transport import (
    ENVIRONMENT_PORT,
    ENVIRONMENT_SERVICE,
    EnvironmentServiceClient,
)
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_SVID_SECRET, RUNTIME_PROXY_TEST_READER_SECRET
from agentplane.system_tests.driver.tls import service_svid_credentials

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

DISPATCHER_SERVICE = "sessions"
HISTORY_SERVICE = "history-service"
SERVICE_PORT = 8080
REQUEST_TIMEOUT_SECONDS = 30.0
RUNNER_READY_SECONDS = 120.0


@dataclass(frozen=True)
class NativePlatformClients:
    """Real backend clients using each API's admitted fixture service role."""

    cluster: Cluster
    environment: EnvironmentServiceClient
    dispatcher: SessionDispatcherClient
    history: HistoryServiceClient
    control: SandboxControlClient
    host_control: SandboxControlClient

    def runner(self, target: identity_pb2.EnvironmentTarget) -> SandboxRunner:
        """Address this exact Sandbox through the Environments Service."""
        if target.namespace != self.cluster.namespace or not target.name or not target.uid:
            raise ValueError("runner status requires this cluster's complete Environment target")
        destination = identity_pb2.SandboxDestination(
            owner=identity_pb2.ServiceAccount(namespace=target.namespace, name=target.name), target=target
        )
        return SandboxRunner(self.control, destination, host_control=self.host_control)

    async def ready_runner(self, target: identity_pb2.EnvironmentTarget) -> SandboxRunner:
        """Wait for this exact Runner using only retry-safe status reads."""
        async with asyncio.timeout(RUNNER_READY_SECONDS):
            while True:
                runner = self.runner(target)
                try:
                    await runner.status()
                    return runner
                except TimeoutError:
                    pass
                except ServiceError as error:
                    if error.code != grpc.StatusCode.UNAVAILABLE:
                        raise
                await asyncio.sleep(0.25)


@asynccontextmanager
async def native_history_client(
    cluster: Cluster, credentials: grpc.ChannelCredentials | None = None
) -> AsyncIterator[HistoryServiceClient]:
    if credentials is None:
        credentials = await service_svid_credentials(cluster, NATIVE_CLIENT_SVID_SECRET)
    async with (
        forward(cluster, f"service/{HISTORY_SERVICE}", remote_port=SERVICE_PORT) as target,
        HistoryServiceClient(
            target,
            request_timeout_s=REQUEST_TIMEOUT_SECONDS,
            channel_credentials=credentials,
            server_name=f"{HISTORY_SERVICE}.{cluster.namespace}.svc.cluster.local",
        ) as history,
    ):
        yield history


@asynccontextmanager
async def native_platform_clients(cluster: Cluster) -> AsyncIterator[NativePlatformClients]:
    """Open native Environment, Dispatcher, History, and Runner-control clients once."""
    async with AsyncExitStack() as opened:
        credentials = await service_svid_credentials(cluster, NATIVE_CLIENT_SVID_SECRET)
        environment_target = await opened.enter_async_context(
            forward(cluster, f"service/{ENVIRONMENT_SERVICE}", remote_port=ENVIRONMENT_PORT)
        )
        environment = await opened.enter_async_context(
            EnvironmentServiceClient.connect(
                environment_target,
                channel_credentials=credentials,
                server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
            )
        )
        dispatcher_target = await opened.enter_async_context(
            forward(cluster, f"service/{DISPATCHER_SERVICE}", remote_port=SERVICE_PORT)
        )
        dispatcher = await opened.enter_async_context(
            SessionDispatcherClient.connect(
                dispatcher_target,
                request_timeout_s=REQUEST_TIMEOUT_SECONDS,
                channel_credentials=credentials,
                server_name=f"{DISPATCHER_SERVICE}.{cluster.namespace}.svc.cluster.local",
            )
        )
        history = await opened.enter_async_context(native_history_client(cluster, credentials))
        yield NativePlatformClients(
            cluster=cluster,
            environment=environment,
            dispatcher=dispatcher,
            history=history,
            control=SandboxControlClient(
                control_target=environment_target,
                request_timeout_s=REQUEST_TIMEOUT_SECONDS,
                channel_credentials=await service_svid_credentials(cluster, RUNTIME_PROXY_TEST_READER_SECRET),
                server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
            ),
            host_control=SandboxControlClient(
                control_target=environment_target,
                request_timeout_s=REQUEST_TIMEOUT_SECONDS,
                channel_credentials=await service_svid_credentials(cluster, "sessions-client-workload-tls"),
                server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
            ),
        )


@asynccontextmanager
async def sandbox_harness_host(cluster: Cluster) -> AsyncIterator[SandboxControlClient]:
    """Open the session reader's authenticated HarnessHost route for exact Sandbox status."""
    credentials = await service_svid_credentials(cluster, "sessions-client-workload-tls")
    async with forward(cluster, f"service/{ENVIRONMENT_SERVICE}", remote_port=ENVIRONMENT_PORT) as target:
        yield SandboxControlClient(
            control_target=target,
            request_timeout_s=REQUEST_TIMEOUT_SECONDS,
            channel_credentials=credentials,
            server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
        )
