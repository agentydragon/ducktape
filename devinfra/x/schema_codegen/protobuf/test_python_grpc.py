import grpc
import pytest_bazel

from devinfra.x.schema_codegen.protobuf.python_grpc_consumer import make_sample_stub


def test_generated_python_grpc_stub_can_be_constructed_without_network() -> None:
    channel = grpc.insecure_channel("127.0.0.1:1")
    try:
        stub = make_sample_stub(channel)
        assert callable(stub.GetSample)
    finally:
        channel.close()


if __name__ == "__main__":
    pytest_bazel.main()
