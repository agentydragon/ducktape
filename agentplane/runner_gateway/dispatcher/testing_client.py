"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import call_unary
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc

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

    async def submit_input(
        self, session_id: str, command_id: str, sandbox: str, sandbox_uid: str, text: str
    ) -> session_dispatcher_pb2.Dispatch:
        """Submit against the caller's pinned Sandbox incarnation; preserve the id on retries."""
        response = await self.unary(
            self.stub.SubmitInput,
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id, command_id=command_id, sandbox=sandbox, sandbox_uid=sandbox_uid, text=text
            ),
        )
        return response.dispatch

    async def session_target(self, session_id: str) -> tuple[str, str]:
        """Read the opening's immutable destination, never the current Sandbox by name."""
        response = await self.unary(
            self.stub.GetSessionOpening, session_dispatcher_pb2.GetSessionOpeningRequest(session_id=session_id)
        )
        opening = response.opening
        if not opening.sandbox or not opening.sandbox_uid:
            raise ValueError(f"Session {session_id} has no recorded Sandbox destination")
        return opening.sandbox, opening.sandbox_uid
