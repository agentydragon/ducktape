"""The client against a bare gRPC server that answers like a truncated Runner Gateway."""

from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.testing.kubernetes import DESTINATION
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf

TOKEN = "test-grpc-token"


async def test_bare_service_eof_is_not_native_closure(tmp_path: Path) -> None:
    async def truncated(
        request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[protocol_pb2.FollowSessionResponse]:
        yield protocol_pb2.FollowSessionResponse(
            attached=runner_pb2.Attached(session_id=request.destination.session_id)
        )
        # Deliberately no ended observation: even an OK transport status is not native EOF evidence.

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                "ducktape.agentplane.runner_gateway.v1.RunnerGateway",
                {
                    "FollowSession": grpc.unary_stream_rpc_method_handler(
                        truncated,
                        request_deserializer=protocol_pb2.FollowSessionRequest.FromString,
                        response_serializer=protocol_pb2.FollowSessionResponse.SerializeToString,
                    )
                },
            )
        ]
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    client = SandboxServiceClient(
        f"127.0.0.1:{port}",
        gateway_target=f"127.0.0.1:{port}",
        namespace=SANDBOX_NAMESPACE,
        token_file=token_file,
        command_admission_timeout_s=20,
        request_timeout_s=20,
        lifecycle_timeout_s=310,
        follow_timeout_s=960,
    )
    try:
        attachment = await client.runner(DESTINATION).attach("session")
        try:
            with pytest.raises(ConnectionError, match="without native closure"):
                await attachment.next_entry()
        finally:
            attachment.cancel()
    finally:
        await client.close()
        await server.stop(0)


if __name__ == "__main__":
    pytest_bazel.main()
