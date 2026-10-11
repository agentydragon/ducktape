"""The client against a bare gRPC server that answers like a truncated Runner Gateway."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.grpc_process import serving
from agentplane.proto import runner_pb2
from agentplane.runner_gateway import protocol_pb2_grpc
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.testing.grpc_service import connect
from agentplane.sandbox_service.testing.kubernetes import DESTINATION
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf

TOKEN = "test-grpc-token"


class TruncatedGateway(protocol_pb2_grpc.RunnerGatewayServicer):
    @override
    async def FollowSession(
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        yield protocol_pb2.FollowSessionResponse(
            attached=runner_pb2.Attached(session_id=request.destination.session_id)
        )
        # Deliberately no ended observation: even an OK transport status is not native EOF evidence.


async def test_bare_service_eof_is_not_native_closure(tmp_path: Path) -> None:
    server = grpc.aio.server()
    protocol_pb2_grpc.add_RunnerGatewayServicer_to_server(TruncatedGateway(), server)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        client = connect(f"127.0.0.1:{port}", token_file, namespace=SANDBOX_NAMESPACE, command_admission_timeout_s=20)
        try:
            attachment = await client.runner(DESTINATION).attach("session")
            try:
                with pytest.raises(ConnectionError, match="without native closure"):
                    await attachment.next_entry()
            finally:
                attachment.cancel()
        finally:
            await client.close()


if __name__ == "__main__":
    pytest_bazel.main()
