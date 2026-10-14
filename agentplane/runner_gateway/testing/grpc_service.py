"""Start the Runner Gateway on a loopback ephemeral port for API tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.grpc_calls import dial
from agentplane.grpc_serving import serving
from agentplane.runner_gateway import grpc_api as gateway_api, runner_proxy
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient

# gazelle:include_dep @pypi//grpcio


@asynccontextmanager
async def service(resources: gateway_api.Resources) -> AsyncIterator[str]:
    server = grpc.aio.server()
    gateway_api.add_service(resources, server)
    server.add_generic_rpc_handlers(
        (
            runner_proxy.RunnerProxy(
                runner_proxy.Resources(
                    principals=resources.principals,
                    caller_accounts=resources.caller_accounts,
                    sandboxes=resources.destinations.sandboxes,
                    control=resources.control,
                    sandbox_controller_account=None,
                )
            ),
        )
    )
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield f"127.0.0.1:{port}"


def control_client(token_file: Path, control_target: str) -> SandboxControlClient:
    return SandboxControlClient(
        control_target=control_target, token_file=token_file, request_timeout_s=20, lifecycle_timeout_s=310
    )


def gateway_client(
    channel: grpc.aio.Channel, token_file: Path, *, command_admission_timeout_s: float, control_target: str
) -> RunnerGatewayClient:
    return RunnerGatewayClient(
        channel,
        token_file=token_file,
        command_admission_timeout_s=command_admission_timeout_s,
        request_timeout_s=20,
        control=control_client(token_file, control_target),
    )


@asynccontextmanager
async def connect(
    target: str, token_file: Path, *, command_admission_timeout_s: float, control_target: str
) -> AsyncIterator[RunnerGatewayClient]:
    """The client of the gateway at `target`, with the control Service at `control_target`."""
    async with dial(target) as channel:
        yield gateway_client(
            channel, token_file, command_admission_timeout_s=command_admission_timeout_s, control_target=control_target
        )


@asynccontextmanager
async def service_client(resources: gateway_api.Resources, token_file: Path) -> AsyncIterator[RunnerGatewayClient]:
    async with (
        service(resources) as target,
        connect(
            target,
            token_file,
            command_admission_timeout_s=resources.runner_admission_ack_timeout_s + 10,
            control_target=target,
        ) as client,
    ):
        yield client
