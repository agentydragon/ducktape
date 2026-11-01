"""Test transport to the native Session Dispatcher with an existing bound service identity."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.port_forward import PortForward, async_port_forward
from agentplane.system_tests.tracing import tracer

SERVICE = "session-dispatcher"
TOKEN_AUDIENCE = "session-dispatcher"


@dataclass(frozen=True)
class Caller:
    token: str = field(repr=False)

    @property
    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),)


async def bound_caller(cluster: Cluster, service_account: str) -> tuple[Caller, str, str]:
    """Mint a Dispatcher-audience token bound to an existing service Pod."""
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
            f"--audience={TOKEN_AUDIENCE}",
            "--duration=1800s",
            "--bound-object-kind=Pod",
            f"--bound-object-name={name}",
            f"--bound-object-uid={uid}",
        )
        return Caller(token), name, uid


@asynccontextmanager
async def forward(cluster: Cluster, resource: str = f"service/{SERVICE}") -> AsyncIterator[str]:
    """Port-forward the native gRPC endpoint; the caller owns the process lifetime."""
    port_forward = PortForward(
        cluster.command("port-forward"),
        resource=resource,
        local_address="127.0.0.1",
        local_port=None,
        remote_port=8080,
        readiness_timeout_s=30,
    )
    async with async_port_forward(port_forward) as target:
        yield target


@asynccontextmanager
async def native_client(cluster: Cluster, token_file: Path) -> AsyncIterator[SessionDispatcherClient]:
    """Own a native channel using the existing backend-authorized test principal."""
    caller, _, _ = await bound_caller(cluster, "app")
    await asyncio.to_thread(token_file.write_text, caller.token)
    async with (
        forward(cluster) as target,
        SessionDispatcherClient.connect(target, token_file=token_file, request_timeout_s=30) as client,
    ):
        yield client
