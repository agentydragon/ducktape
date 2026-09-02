"""Small Python gRPC backend for the Bazel gRPC demo."""

import logging
import os
from collections.abc import Callable, Iterable
from concurrent import futures
from typing import override

import greeting_pb2
import greeting_pb2_grpc
import grpc

from x.grpc_demo.auth import InvalidAccessTokenError, OidcTokenVerifier

logger = logging.getLogger(__name__)


def _bearer_token(metadata: Iterable[tuple[str, str | bytes]]) -> str:
    """Extract exactly one HTTP Bearer credential from gRPC metadata."""
    authorization = [value for key, value in metadata if key.casefold() == "authorization"]
    if len(authorization) != 1 or not isinstance(authorization[0], str):
        raise InvalidAccessTokenError
    scheme, separator, token = authorization[0].partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token.strip():
        raise InvalidAccessTokenError
    return token.strip()


class Greeter(greeting_pb2_grpc.GreeterServicer):
    """Implement the service declared in greeting.proto."""

    def __init__(self, verify_access_token: Callable[[str], object] | None = None) -> None:
        self._verify_access_token = verify_access_token

    @override
    def SayHello(self, request: greeting_pb2.HelloRequest, context: grpc.ServicerContext) -> greeting_pb2.HelloReply:
        if self._verify_access_token is not None:
            try:
                self._verify_access_token(_bearer_token(context.invocation_metadata()))
            except InvalidAccessTokenError:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "valid bearer access token required")
        name = request.name.strip()
        if not name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name must not be empty")
        return greeting_pb2.HelloReply(message=f"Hello, {name}!")


def create_server(verify_access_token: Callable[[str], object] | None = None) -> grpc.Server:
    """Create an unstarted server with the Greeter service registered."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    greeting_pb2_grpc.add_GreeterServicer_to_server(Greeter(verify_access_token), server)
    return server


def main() -> None:
    port = int(os.environ.get("GRPC_DEMO_PORT", "50051"))
    if os.environ.get("GRPC_DEMO_ALLOW_ANONYMOUS", "").casefold() == "true":
        logger.warning("gRPC demo authentication is disabled; use only for local development")
        verify_access_token = None
    else:
        verify_access_token = OidcTokenVerifier.from_environment()
    server = create_server(verify_access_token)
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
