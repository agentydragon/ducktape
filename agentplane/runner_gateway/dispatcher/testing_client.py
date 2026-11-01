"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import grpc
from google.protobuf.message import Message
from tenacity import AsyncRetrying, RetryError, retry_if_result, stop_after_delay, wait_fixed

from agentplane.grpc.calls import call_unary, dial
from agentplane.proto import session_pb2
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

    @classmethod
    @asynccontextmanager
    async def connect(cls, target: str, *, token_file: Path, request_timeout_s: float) -> AsyncIterator[Self]:
        """Own the channel for this client's lifetime; callers select credentials and deadline."""
        async with dial(target) as channel:
            yield cls(channel, token_file=token_file, request_timeout_s=request_timeout_s)

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

    async def open_session(
        self,
        session_id: str,
        sandbox: str,
        sandbox_uid: str,
        spec: session_pb2.SessionSpec,
        setup_script: str | None = None,
    ) -> session_dispatcher_pb2.SessionOpening:
        """Submit one durable opening with its caller-minted identity and complete spec.

        This mutation is deliberately issued once. On an ambiguous transport result the caller
        must reconcile the same Session ID with `get_session_opening`, never replay the write here.
        """
        request = session_dispatcher_pb2.OpenSessionRequest(
            session_id=session_id, sandbox=sandbox, sandbox_uid=sandbox_uid, spec=spec
        )
        if setup_script is not None:
            request.setup_script = setup_script
        response = await self.unary(self.stub.OpenSession, request)
        if not response.HasField("opening"):
            raise ValueError("OpenSession returned no opening")
        return response.opening

    async def get_session_opening(self, session_id: str) -> session_dispatcher_pb2.SessionOpening:
        """Reconcile one previously submitted opening by its stable Session ID."""
        response = await self.unary(
            self.stub.GetSessionOpening, session_dispatcher_pb2.GetSessionOpeningRequest(session_id=session_id)
        )
        if not response.HasField("opening"):
            raise ValueError("GetSessionOpening returned no opening")
        return response.opening

    async def wait_for_opening(
        self, opening: session_dispatcher_pb2.SessionOpening, *, timeout_s: float
    ) -> session_dispatcher_pb2.SessionOpening:
        """Poll only the opening receipt until the Runner admits it or the opening fails."""

        def raise_if_terminal(current: session_dispatcher_pb2.SessionOpening) -> None:
            if current.state in (
                session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
            ):
                raise RuntimeError(f"Session opening {current.session_id} ended in {current.state}: {current.reason}")

        if opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED:
            return opening
        raise_if_terminal(opening)

        async def read_opening() -> session_dispatcher_pb2.SessionOpening:
            current = await self.get_session_opening(opening.session_id)
            raise_if_terminal(current)
            return current

        try:
            return await AsyncRetrying(
                stop=stop_after_delay(timeout_s),
                wait=wait_fixed(0.2),
                retry=retry_if_result(
                    lambda current: current.state != session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
                ),
            )(read_opening)
        except RetryError as error:
            current = error.last_attempt.result()
            raise TimeoutError(
                f"Session opening {current.session_id} remained {current.state}: {current.reason}"
            ) from error

    async def session_target(self, session_id: str) -> tuple[str, str]:
        """Read the opening's immutable destination, never the current Sandbox by name."""
        opening = await self.get_session_opening(session_id)
        if not opening.sandbox or not opening.sandbox_uid:
            raise ValueError(f"Session {session_id} has no recorded Sandbox destination")
        return opening.sandbox, opening.sandbox_uid
