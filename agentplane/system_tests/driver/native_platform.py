"""Native clients and exact control routing for system-test platform scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import identity_pb2
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import (
    GATEWAY_PORT,
    GATEWAY_SERVICE,
    GATEWAY_TLS_SERVICE,
    EnvironmentServiceClient,
)
from agentplane.system_tests.driver.native_auth import write_bound_fixture_token
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.tls import service_credentials

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

DISPATCHER_SERVICE = "session-dispatcher"
DISPATCHER_AUDIENCE = "session-dispatcher"
GATEWAY_AUDIENCE = "agentplane-runner-gateway"
HISTORY_SERVICE = "history-service"
HISTORY_AUDIENCE = "agentplane-history-service"
SERVICE_PORT = 8080
REQUEST_TIMEOUT_SECONDS = 30.0
RUNNER_READY_SECONDS = 120.0


@dataclass(frozen=True)
class NativePlatformClients:
    """Real backend clients authenticated as the fixture-owned native principal."""

    cluster: Cluster
    environment: EnvironmentServiceClient
    dispatcher: SessionDispatcherClient
    history: HistoryServiceClient
    control: SandboxControlClient

    async def runner(self, target: identity_pb2.EnvironmentTarget) -> SandboxRunner:
        """Address this exact Sandbox through the stable Gateway Service."""
        if target.namespace != self.cluster.namespace or not target.name or not target.uid:
            raise ValueError("runner status requires this cluster's complete Environment target")
        destination = identity_pb2.SandboxDestination(
            owner=identity_pb2.ServiceAccount(namespace=target.namespace, name=target.name), target=target
        )
        return self.control.runner(destination)

    async def ready_runner(
        self, target: identity_pb2.EnvironmentTarget, *, timeout_s: float = RUNNER_READY_SECONDS
    ) -> SandboxRunner:
        """Wait for this exact Runner using only retry-safe status reads."""
        async with asyncio.timeout(timeout_s):
            while True:
                try:
                    runner = await self.runner(target)
                    await runner.status()
                    return runner
                except TimeoutError:
                    pass
                except ServiceError as error:
                    if error.code != grpc.StatusCode.UNAVAILABLE:
                        raise
                await asyncio.sleep(0.25)


@asynccontextmanager
async def native_platform_clients(cluster: Cluster, directory: Path) -> AsyncIterator[NativePlatformClients]:
    """Open native Environment, Dispatcher, History, and Runner-control clients once."""
    await asyncio.to_thread(directory.mkdir, mode=0o700, parents=True, exist_ok=True)
    gateway_token = directory / "gateway-token"
    dispatcher_token = directory / "dispatcher-token"
    history_token = directory / "history-token"

    # All credentials are bound to the same fixture Pod UID. EnvironmentService shares the
    # Dispatcher's method audience; the gateway token is only for SandboxControl reads.
    principal = await write_bound_fixture_token(cluster, gateway_token, audience=GATEWAY_AUDIENCE)
    dispatcher_principal = await write_bound_fixture_token(cluster, dispatcher_token, audience=DISPATCHER_AUDIENCE)
    history_principal = await write_bound_fixture_token(cluster, history_token, audience=HISTORY_AUDIENCE)
    if dispatcher_principal != principal or history_principal != principal:
        raise RuntimeError("native client tokens were not bound to one fixture Pod incarnation")

    async with AsyncExitStack() as opened:
        credentials = await service_credentials(cluster)
        gateway_target = await opened.enter_async_context(_service_forward(cluster, GATEWAY_SERVICE, GATEWAY_PORT))
        environment = await opened.enter_async_context(
            EnvironmentServiceClient.connect(
                gateway_target,
                token_file=dispatcher_token,
                channel_credentials=credentials,
                server_name=f"{GATEWAY_TLS_SERVICE}.{cluster.namespace}.svc.cluster.local",
            )
        )
        dispatcher_target = await opened.enter_async_context(
            _service_forward(cluster, DISPATCHER_SERVICE, SERVICE_PORT)
        )
        dispatcher = await opened.enter_async_context(
            SessionDispatcherClient.connect(
                dispatcher_target,
                token_file=dispatcher_token,
                request_timeout_s=REQUEST_TIMEOUT_SECONDS,
                channel_credentials=credentials,
                server_name=f"{DISPATCHER_SERVICE}.{cluster.namespace}.svc.cluster.local",
            )
        )
        history_target = await opened.enter_async_context(_service_forward(cluster, HISTORY_SERVICE, SERVICE_PORT))
        history = HistoryServiceClient(
            history_target,
            token_file=history_token,
            request_timeout_s=REQUEST_TIMEOUT_SECONDS,
            channel_credentials=credentials,
            server_name=f"{HISTORY_SERVICE}.{cluster.namespace}.svc.cluster.local",
        )
        await opened.enter_async_context(history)
        yield NativePlatformClients(
            cluster=cluster,
            environment=environment,
            dispatcher=dispatcher,
            history=history,
            control=SandboxControlClient(
                control_target=gateway_target,
                token_file=gateway_token,
                request_timeout_s=REQUEST_TIMEOUT_SECONDS,
                channel_credentials=credentials,
                server_name=f"{GATEWAY_TLS_SERVICE}.{cluster.namespace}.svc.cluster.local",
            ),
        )


@asynccontextmanager
async def _service_forward(cluster: Cluster, service: str, port: int) -> AsyncIterator[str]:
    forward = PortForward(
        cluster.command("port-forward"),
        resource=f"service/{service}",
        local_address="127.0.0.1",
        local_port=None,
        remote_port=port,
        readiness_timeout_s=30,
    )
    async with async_port_forward(forward) as target:
        yield target
