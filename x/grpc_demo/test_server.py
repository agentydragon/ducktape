"""Behavioral test for the Python gRPC service."""

from typing import cast

import greeting_pb2
import greeting_pb2_grpc
import grpc
import pytest
import pytest_bazel

from x.grpc_demo.auth import InvalidAccessTokenError
from x.grpc_demo.server import create_server


def test_say_hello_uses_the_shared_proto() -> None:
    server = create_server()
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"localhost:{port}")
        response = greeting_pb2_grpc.GreeterStub(channel).SayHello(greeting_pb2.HelloRequest(name="Bazel"))
        assert response == greeting_pb2.HelloReply(message="Hello, Bazel!")
        channel.close()
    finally:
        server.stop(grace=0).wait()


@pytest.mark.parametrize("metadata", [(), (("authorization", "Bearer invalid"),)])
def test_say_hello_rejects_missing_or_invalid_access_tokens(metadata: tuple[tuple[str, str], ...]) -> None:
    def verify_access_token(token: str) -> object:
        if token != "valid":
            raise InvalidAccessTokenError
        return object()

    server = create_server(verify_access_token)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"localhost:{port}")
        with pytest.raises(grpc.RpcError) as error:
            greeting_pb2_grpc.GreeterStub(channel).SayHello(greeting_pb2.HelloRequest(name="Bazel"), metadata=metadata)
        assert cast(grpc.Call, error.value).code() == grpc.StatusCode.UNAUTHENTICATED
        channel.close()
    finally:
        server.stop(grace=0).wait()


def test_say_hello_accepts_a_verified_access_token() -> None:
    def verify_access_token(token: str) -> object:
        if token != "valid":
            raise InvalidAccessTokenError
        return object()

    server = create_server(verify_access_token)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"localhost:{port}")
        response = greeting_pb2_grpc.GreeterStub(channel).SayHello(
            greeting_pb2.HelloRequest(name="Bazel"), metadata=(("authorization", "Bearer valid"),)
        )
        assert response == greeting_pb2.HelloReply(message="Hello, Bazel!")
        channel.close()
    finally:
        server.stop(grace=0).wait()


if __name__ == "__main__":
    pytest_bazel.main()
