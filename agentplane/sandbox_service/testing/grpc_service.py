"""Serve gRPC on a loopback ephemeral port, the Sandbox Service alone or what a caller adds to the
server, and connect the production client to it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.sandbox_service import grpc_api
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//grpcio

# The bearer the `token_file` fixture holds; a test trusts it for the caller it authenticates as.
CALLER_TOKEN = "test-caller-token"


@asynccontextmanager
async def serving(server: grpc.aio.Server) -> AsyncIterator[str]:
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(0)


def connect(
    target: str,
    token_file: Path,
    *,
    namespace: str,
    command_admission_timeout_s: float | None,
    control_target: str | None = None,
) -> SandboxServiceClient:
    """The client of both services at one target."""
    return SandboxServiceClient(
        target,
        gateway_target=target,
        control_target=control_target,
        namespace=namespace,
        token_file=token_file,
        command_admission_timeout_s=command_admission_timeout_s,
        request_timeout_s=20,
        lifecycle_timeout_s=310,
        follow_timeout_s=960,
    )


@asynccontextmanager
async def service_client(resources: grpc_api.Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    server = grpc.aio.server()
    grpc_api.add_service(resources, server)
    async with serving(server) as target:
        client = connect(
            target, token_file, namespace=resources.provisioning.inventory.namespace, command_admission_timeout_s=20
        )
        try:
            yield client
        finally:
            await client.close()
