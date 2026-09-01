"""Small Python gRPC backend for the Bazel gRPC demo."""

import logging
import os
from concurrent import futures
from typing import override

import greeting_pb2
import greeting_pb2_grpc
import grpc

logger = logging.getLogger(__name__)


class Greeter(greeting_pb2_grpc.GreeterServicer):
    """Implement the service declared in greeting.proto."""

    @override
    def SayHello(self, request: greeting_pb2.HelloRequest, context: grpc.ServicerContext) -> greeting_pb2.HelloReply:
        name = request.name.strip()
        if not name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name must not be empty")
        return greeting_pb2.HelloReply(message=f"Hello, {name}!")


def create_server() -> grpc.Server:
    """Create an unstarted server with the Greeter service registered."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    greeting_pb2_grpc.add_GreeterServicer_to_server(Greeter(), server)
    return server


def main() -> None:
    port = int(os.environ.get("GRPC_DEMO_PORT", "50051"))
    server = create_server()
    bound_port = server.add_insecure_port(f"[::]:{port}")
    if bound_port != port:
        raise RuntimeError(f"could not bind gRPC server to port {port}: bound {bound_port}")
    server.start()
    logger.info("gRPC backend listening on port %d", bound_port)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=1).wait()


if __name__ == "__main__":
    main()
