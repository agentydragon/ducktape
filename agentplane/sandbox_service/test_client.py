"""Each service the client reaches is called with its own audience's bearer."""

from pathlib import Path
from typing import override

import grpc
import pytest_bazel

from agentplane.grpc_serving import serving
from agentplane.proto import runner_gateway_pb2, runner_gateway_pb2_grpc
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//protobuf

SERVICE_TOKEN = "test-sandbox-service-audience"
GATEWAY_TOKEN = "test-runner-gateway-audience"


class GatewayAcceptingOnlyItsAudience(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    async def _authenticate(self, context: grpc.aio.ServicerContext) -> None:
        if ("authorization", f"Bearer {GATEWAY_TOKEN}") not in (context.invocation_metadata() or ()):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "wrong audience")

    @override
    async def BatchGetSessionSandboxes(
        self, request: runner_gateway_pb2.BatchGetSessionSandboxesRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SessionSandboxes:
        await self._authenticate(context)
        return runner_gateway_pb2.SessionSandboxes(
            sessions=[runner_gateway_pb2.SessionSandbox(session_id=id, sandbox="box") for id in request.session_ids]
        )

    @override
    async def FindSessions(
        self, request: runner_gateway_pb2.FindSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SessionSandboxes:
        await self._authenticate(context)
        return runner_gateway_pb2.SessionSandboxes(
            sessions=[runner_gateway_pb2.SessionSandbox(session_id="s", sandbox=request.sandbox_name)]
        )


async def test_session_lookups_carry_the_gateway_bearer(tmp_path: Path) -> None:
    (service_token := tmp_path / "service").write_text(SERVICE_TOKEN)
    (gateway_token := tmp_path / "gateway").write_text(GATEWAY_TOKEN)
    server = grpc.aio.server()
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(GatewayAcceptingOnlyItsAudience(), server)
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SandboxServiceClient(
            "127.0.0.1:1",
            gateway_target=f"127.0.0.1:{port}",
            control_target=f"127.0.0.1:{port}",
            namespace="test",
            token_file=service_token,
            gateway_token_file=gateway_token,
            command_admission_timeout_s=None,
            request_timeout_s=5,
            lifecycle_timeout_s=5,
        ) as client,
    ):
        assert [found.sandbox for found in await client.session_sandboxes({"s"})] == ["box"]
        assert [
            found.sandbox
            for found in await client.find_sessions(runner_gateway_pb2.FindSessionsRequest(sandbox_name="haku"))
        ] == ["haku"]


if __name__ == "__main__":
    pytest_bazel.main()
