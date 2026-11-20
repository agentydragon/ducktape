"""Test transport to the native Session Dispatcher with an existing bound service identity."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import grpc

from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.tls import service_svid_credentials
from agentplane.system_tests.driver.tracing import tracer

SERVICE = "sessions"
TOKEN_AUDIENCE = "session-dispatcher"


@dataclass(frozen=True)
class Caller:
    token: str | None = field(default=None, repr=False)
    channel_credentials: grpc.ChannelCredentials | None = field(default=None, repr=False)

    @property
    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),) if self.token is not None else ()


async def bound_caller(
    cluster: Cluster, service_account: str, *, audience: str = TOKEN_AUDIENCE
) -> tuple[Caller, str, str]:
    """Mint an audience-scoped token bound to an existing service Pod."""
    with tracer.start_as_current_span("system_test.dispatcher.caller"):
        pods = json.loads(
            await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={service_account}", "-o", "json")
        )
        pod = next(item for item in pods["items"] if not item["metadata"].get("deletionTimestamp"))
        name = pod["metadata"]["name"]
        uid = pod["metadata"]["uid"]
        token = await cluster.kubectl(
            "create",
            "token",
            service_account,
            f"--audience={audience}",
            "--duration=1800s",
            "--bound-object-kind=Pod",
            f"--bound-object-name={name}",
            f"--bound-object-uid={uid}",
        )
        return Caller(token), name, uid


async def service_caller(cluster: Cluster, component: str, *, certificate_secret: str) -> tuple[Caller, str, str]:
    """Use a role certificate on a real TLS channel; Pod details are diagnostic only."""
    pods = json.loads(await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={component}", "-o", "json"))
    pod = next(item for item in pods["items"] if not item["metadata"].get("deletionTimestamp"))
    credentials = await service_svid_credentials(cluster, certificate_secret)
    return Caller(channel_credentials=credentials), pod["metadata"]["name"], pod["metadata"]["uid"]


@asynccontextmanager
async def forward(
    cluster: Cluster, resource: str = f"service/{SERVICE}", *, remote_port: int = 8080
) -> AsyncIterator[str]:
    """Port-forward the native gRPC endpoint; the caller owns the process lifetime."""
    port_forward = PortForward(
        cluster.command("port-forward"),
        resource=resource,
        local_address="127.0.0.1",
        local_port=None,
        remote_port=remote_port,
        readiness_timeout_s=30,
    )
    async with async_port_forward(port_forward) as target:
        yield target


@asynccontextmanager
async def native_client(cluster: Cluster) -> AsyncIterator[SessionDispatcherClient]:
    """Own a native channel using the existing backend-authorized test principal."""
    caller, _, _ = await service_caller(cluster, "app", certificate_secret="app-workload-tls")
    async with (
        forward(cluster) as target,
        SessionDispatcherClient.connect(
            target,
            request_timeout_s=30,
            channel_credentials=caller.channel_credentials,
            server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
        ) as client,
    ):
        yield client
