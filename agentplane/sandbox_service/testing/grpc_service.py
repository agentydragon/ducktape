"""Serve the Sandbox Service on a loopback ephemeral port and connect the production client to it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.grpc_calls import dial
from agentplane.grpc_serving import serving
from agentplane.sandbox_service import grpc_api
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//grpcio

# The bearer the `token_file` fixture holds; a test trusts it for the caller it authenticates as.
CALLER_TOKEN = "test-caller-token"


def sandbox_client(channel: grpc.aio.Channel, token_file: Path, *, namespace: str) -> SandboxServiceClient:
    return SandboxServiceClient(
        channel, namespace=namespace, token_file=token_file, request_timeout_s=20, lifecycle_timeout_s=310
    )


@asynccontextmanager
async def service_client(resources: grpc_api.Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    server = grpc.aio.server()
    grpc_api.add_service(resources, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield sandbox_client(channel, token_file, namespace=resources.provisioning.inventory.namespace)
