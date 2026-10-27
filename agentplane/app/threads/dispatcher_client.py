"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.app.grpc_stream import OpenWatch, open_watch
from agentplane.grpc.calls import bearer, call_unary
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2_grpc


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

    async def open_watch[RequestT: Message, ResponseT: Message](
        self, call: Callable[..., grpc.aio.UnaryStreamCall[RequestT, ResponseT]], request: RequestT
    ) -> OpenWatch[ResponseT]:
        """Open a browser relay watch with a fresh projected token and bounded first frame."""
        return await open_watch(call, request, metadata=await bearer(self.token_file), timeout_s=self.request_timeout_s)
