"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import call_unary
from agentplane.proto import session_dispatcher_pb2_grpc

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf


class SessionDispatcherClient:
    """Keep transport concerns beside the generated stub, not RPC business logic."""

    def __init__(self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float) -> None:
        self.stub: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub = (
            session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        )
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s

    async def unary[ResponseT: Message](self, call: Callable[..., Awaitable[ResponseT]], request: Message) -> ResponseT:
        return await call_unary(call, request, token_file=self.token_file, timeout_s=self.request_timeout_s)
