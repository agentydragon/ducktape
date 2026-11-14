"""Native clients and exact control routing for system-test platform scenarios."""

from __future__ import annotations

import asyncio
import json
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
    GATEWAY_AUDIENCE,
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
HISTORY_SERVICE = "history-service"
HISTORY_AUDIENCE = "agentplane-history-service"
SERVICE_PORT = 8080
REQUEST_TIMEOUT_SECONDS = 30.0
RUNNER_READY_SECONDS = 120.0


class RunnerEndpointNotReadyError(RuntimeError):
    """The exact Sandbox's Gateway-owned control endpoint has not been published yet."""


@dataclass(frozen=True)
class NativePlatformClients:
    """Real backend clients authenticated as the fixture-owned native principal."""

    cluster: Cluster
    environment: EnvironmentServiceClient
    dispatcher: SessionDispatcherClient
    history: HistoryServiceClient
    gateway_token_file: Path
    credentials: grpc.ChannelCredentials

    async def runner(self, target: identity_pb2.EnvironmentTarget) -> SandboxRunner:
        """Resolve the Sandbox-owned Gateway endpoint for this exact Sandbox incarnation."""
        if target.namespace != self.cluster.namespace or not target.name or not target.uid:
            raise ValueError("runner status requires this cluster's complete Environment target")
        control_target = await sandbox_control_endpoint(self.cluster, target)
        control = SandboxControlClient(
            control_target=control_target,
            token_file=self.gateway_token_file,
            request_timeout_s=REQUEST_TIMEOUT_SECONDS,
            channel_credentials=self.credentials,
            server_name=f"{GATEWAY_TLS_SERVICE}.{self.cluster.namespace}.svc.cluster.local",
        )
        destination = identity_pb2.SandboxDestination(
            owner=identity_pb2.ServiceAccount(namespace=target.namespace, name=target.name), target=target
        )
        return control.runner(destination)

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
                except RunnerEndpointNotReadyError:
                    pass
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

    # All credentials are bound to the same fixture Pod UID. The gateway token also authorizes
    # Environment mutations and SandboxControl reads; the other tokens have narrower audiences.
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
                token_file=gateway_token,
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
            gateway_token_file=gateway_token,
            credentials=credentials,
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


async def sandbox_control_endpoint(cluster: Cluster, target: identity_pb2.EnvironmentTarget) -> str:
    """Return the sole ready endpoint owned by the exact Sandbox's Gateway control Service."""
    if target.namespace != cluster.namespace or not target.name or not target.uid:
        raise ValueError("control routing requires this cluster's complete Environment target")
    response = json.loads(
        await cluster.kubectl(
            "get", "endpointslices", "-l", f"kubernetes.io/service-name={target.name}-control", "-o", "json"
        )
    )
    slices = response["items"]
    if not slices:
        raise RunnerEndpointNotReadyError(f"no Gateway control EndpointSlice for {target.name}/{target.uid}")
    if len(slices) != 1:
        raise RuntimeError(f"expected one control EndpointSlice for {target.name}/{target.uid}, found {len(slices)}")
    (slice_,) = slices
    metadata = slice_["metadata"]
    if metadata.get("labels", {}).get("endpointslice.kubernetes.io/managed-by") != "agentplane-runner-gateway":
        raise RuntimeError(f"control EndpointSlice for {target.name}/{target.uid} is not Gateway-owned")
    if not any(
        owner.get("kind") == "Sandbox" and owner.get("name") == target.name and owner.get("uid") == target.uid
        for owner in metadata.get("ownerReferences", [])
    ):
        raise RuntimeError(f"control EndpointSlice is not owned by exact Sandbox {target.name}/{target.uid}")
    ready = [endpoint for endpoint in slice_.get("endpoints", []) if endpoint.get("conditions", {}).get("ready")]
    if not ready:
        raise RunnerEndpointNotReadyError(f"no ready Gateway control endpoint for {target.name}/{target.uid}")
    if len(ready) != 1:
        raise RuntimeError(f"expected one ready control endpoint for {target.name}/{target.uid}, found {len(ready)}")
    (endpoint,) = ready
    addresses = endpoint.get("addresses", [])
    if len(addresses) != 1:
        raise RuntimeError(f"expected one address for {target.name}/{target.uid}, found {len(addresses)}")
    (address,) = addresses
    ports = slice_.get("ports", [])
    if len(ports) != 1:
        raise RuntimeError(f"expected one control port for {target.name}/{target.uid}, found {len(ports)}")
    (port,) = ports
    if port.get("protocol", "TCP") != "TCP":
        raise RuntimeError(f"control EndpointSlice for {target.name}/{target.uid} is not TCP")
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port['port']}"
