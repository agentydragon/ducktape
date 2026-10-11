"""Start the Runner Gateway on a loopback ephemeral port and, given a `Provisioning`, the Sandbox
Service beside it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.grpc_serving import serving
from agentplane.runner_gateway import grpc_api as gateway_api
from agentplane.sandbox_service import grpc_api as sandbox_api
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.grpc_service import connect

# gazelle:include_dep @pypi//grpcio


@asynccontextmanager
async def service(resources: gateway_api.Resources, provisioning: Provisioning | None = None) -> AsyncIterator[str]:
    server = grpc.aio.server()
    gateway_api.add_service(resources, server)
    if provisioning is not None:
        # The Sandbox Service, for the same callers and deadlines as the gateway.
        sandbox_api.add_service(
            sandbox_api.Resources(
                principals=resources.principals,
                provisioning=provisioning,
                caller_accounts=resources.caller_accounts,
                admission_timeout_s=resources.admission_timeout_s,
                lifecycle_timeout_s=resources.lifecycle_timeout_s,
            ),
            server,
        )
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield f"127.0.0.1:{port}"


@asynccontextmanager
async def service_client(
    resources: gateway_api.Resources, token_file: Path, provisioning: Provisioning | None = None
) -> AsyncIterator[SandboxServiceClient]:
    async with (
        service(resources, provisioning) as target,
        connect(
            target,
            token_file,
            namespace=resources.destinations.sandboxes.namespace,
            command_admission_timeout_s=resources.runner_admission_ack_timeout_s + 10,
            control_target=target,
        ) as client,
    ):
        yield client
