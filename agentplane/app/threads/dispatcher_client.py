"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from uuid import UUID

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import bearer, call_unary, raise_status
from agentplane.proto import session_dispatcher_pb2, session_dispatcher_pb2_grpc

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

    async def watch_dispatches(self, session_id: UUID) -> AsyncIterator[None]:
        """Yield once per Dispatch of the Session that changed, until cancelled; anything else raises.

        The Dispatcher ends each stream on a lease, which this renews. A new stream starts with every
        Dispatch of the Session again, so a renewal yields once more for each of them.
        """
        request = session_dispatcher_pb2.WatchDispatchesRequest(
            filter=session_dispatcher_pb2.DispatchFilter(session_id=str(session_id))
        )
        while True:
            # A long-lived stream: no call deadline.
            call = self.stub.WatchDispatches(request, metadata=await bearer(self.token_file))
            try:
                while True:
                    try:
                        message = await call.read()
                    except grpc.aio.AioRpcError as error:
                        raise_status(error)
                    if message is grpc.aio.EOF:
                        break
                    yield
            finally:
                call.cancel()
