"""Generated Dispatcher stub with rotating bearer and deadline transport."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self
from uuid import UUID

import grpc
from google.protobuf.message import Message
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.grpc.calls import bearer, call_unary, dial
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
                session_id=session_id, command_id=command_id, expected_sandbox_uid=sandbox_uid, text=text
            ),
        )
        return response.dispatch

    async def get_session_opening(self, session_id: str) -> session_dispatcher_pb2.SessionOpening:
        """Read one durable opening by its globally unique Session ID."""
        response = await self.unary(
            self.stub.GetSessionOpening, session_dispatcher_pb2.GetSessionOpeningRequest(session_id=session_id)
        )
        return response.opening

    async def session_target(self, session_id: str) -> tuple[str, str]:
        """Read the opening's immutable destination, never the current Sandbox by name."""
        opening = await self.get_session_opening(session_id)
        match opening.placement.WhichOneof("kind"):
            case "environment_hosted":
                target = opening.placement.environment_hosted.environment
            case "central_sampler_v1":
                target = opening.placement.central_sampler_v1.executor_environment
            case _:
                raise ValueError(f"Session {session_id} has no recorded placement")
        if not target.name or not target.uid:
            raise ValueError(f"Session {session_id} has no recorded Sandbox destination")
        return target.name, target.uid


async def delivered_opening(dispatcher: SessionDispatcherClient, session_id: UUID) -> None:
    """Return once the Dispatcher has delivered the Session's opening to its runner."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            response = await dispatcher.unary(
                dispatcher.stub.GetSessionOpening,
                session_dispatcher_pb2.GetSessionOpeningRequest(session_id=str(session_id)),
            )
            assert response.opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, response.opening


async def admitted(
    dispatcher: SessionDispatcherClient, dispatch: session_dispatcher_pb2.Dispatch
) -> session_dispatcher_pb2.Dispatch:
    """`dispatch` once its runner admitted it, as the Dispatcher's watch reports it."""
    stream = dispatcher.stub.WatchDispatches(
        session_dispatcher_pb2.WatchDispatchesRequest(
            filter=session_dispatcher_pb2.DispatchFilter(
                session_id=dispatch.session_id, command_ids=[dispatch.command_id]
            )
        ),
        metadata=await bearer(dispatcher.token_file),
        timeout=30,
    )
    try:
        async for response in stream:
            if not response.HasField("dispatch"):
                continue
            row: session_dispatcher_pb2.Dispatch = response.dispatch
            assert row.state not in {
                session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
            }, row
            if row.state in {
                session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
                session_dispatcher_pb2.DELIVERY_STATE_HARNESS_CONFIRMED,
            }:
                return row
    finally:
        stream.cancel()
    raise AssertionError("the Dispatch watch ended before the runner admitted it")
