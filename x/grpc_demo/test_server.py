"""Behavioral test for the Python gRPC service."""

import greeting_pb2
import greeting_pb2_grpc
import grpc
import pytest_bazel

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


if __name__ == "__main__":
    pytest_bazel.main()
