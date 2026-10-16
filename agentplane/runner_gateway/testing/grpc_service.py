"""Start the Runner proxy on a loopback ephemeral port for API tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.grpc.serving import serving
from agentplane.runner_gateway import runner_proxy
from agentplane.runner_gateway.client import SandboxControlClient

# gazelle:include_dep @pypi//grpcio


@asynccontextmanager
async def service(resources: runner_proxy.Resources) -> AsyncIterator[str]:
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((runner_proxy.RunnerProxy(resources),))
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield f"127.0.0.1:{port}"


@asynccontextmanager
async def service_client(resources: runner_proxy.Resources, token_file: Path) -> AsyncIterator[SandboxControlClient]:
    async with service(resources) as target:
        yield SandboxControlClient(
            control_target=target, token_file=token_file, request_timeout_s=20, lifecycle_timeout_s=310
        )
