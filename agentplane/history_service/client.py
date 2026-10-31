"""gRPC client for History Service reads, with no Kubernetes fallback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import bearer, raise_status
from agentplane.grpc.options import LARGE_EVENT_RECEIVE_OPTIONS
from agentplane.history_service import history_service_pb2, history_service_pb2_grpc


@dataclass(frozen=True)
class SessionOpened:
    session_id: UUID
    # Recording order; a watch resumed after it misses no later Session.
    position: int


class HistoryServiceClient:
    def __init__(self, target: str, *, token_file: Path, request_timeout_s: float) -> None:
        self.target = target
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: history_service_pb2_grpc.HistoryServiceAsyncStub | None = None

    @property
    def stub(self) -> history_service_pb2_grpc.HistoryServiceAsyncStub:
        if self._stub is None:
            # A local subchannel pool makes a channel after `redial()` dial anew; from the process-wide
            # pool it would inherit the closed channel's connection.
            self._channel = grpc.aio.insecure_channel(
                self.target, options=(*LARGE_EVENT_RECEIVE_OPTIONS, ("grpc.use_local_subchannel_pool", 1))
            )
            self._stub = history_service_pb2_grpc.HistoryServiceStub(self._channel)
        return self._stub

    async def unary[T](self, call: Callable[..., Awaitable[T]], request: Message) -> T:
        try:
            return await call(request, metadata=await bearer(self.token_file), timeout=self.request_timeout_s)
        except grpc.aio.AioRpcError as error:
            raise_status(error)

    def new_client(self) -> HistoryServiceClient:
        """Create an independent channel with this client's endpoint and credentials."""
        return HistoryServiceClient(self.target, token_file=self.token_file, request_timeout_s=self.request_timeout_s)

    async def read_session_events(
        self, session_id: str, *, after_cursor: int = 0, limit: int = 128
    ) -> history_service_pb2.ReadSessionEventsResponse:
        if not session_id or after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history page")
        return await self.unary(
            self.stub.ReadSessionEvents,
            history_service_pb2.ReadSessionEventsRequest(session_id=session_id, after_cursor=after_cursor, limit=limit),
        )

    async def follow_session(
        self, session_id: str, *, after_cursor: int = 0
    ) -> AsyncGenerator[history_service_pb2.FollowSessionResponse]:
        """Yield committed raw history until the server asks the caller to reconnect."""
        if not session_id or after_cursor < 0:
            raise ValueError("invalid session history cursor")
        request = history_service_pb2.FollowSessionRequest(
            session_id=session_id, after_cursor=after_cursor, follow=True
        )
        call = self.stub.FollowSession(request, metadata=await bearer(self.token_file))
        handshake: float | None = self.request_timeout_s
        try:
            while True:
                try:
                    async with asyncio.timeout(handshake):
                        response = await call.read()
                except grpc.aio.AioRpcError as error:
                    raise_status(error)
                handshake = None
                if response is grpc.aio.EOF:
                    raise ConnectionError("History Service ended the raw history follow without planned renewal")
                assert isinstance(response, history_service_pb2.FollowSessionResponse)
                yield response
                if response.HasField("reconnect_required"):
                    return
        finally:
            call.cancel()

    async def get_session_summary(self, session_id: str) -> history_service_pb2.SessionSummary:
        return await self.unary(
            self.stub.GetSessionSummary, history_service_pb2.GetSessionSummaryRequest(session_id=session_id)
        )

    async def read_thread_entities(
        self, request: history_service_pb2.ReadThreadEntitiesRequest
    ) -> history_service_pb2.ReadThreadEntitiesResponse:
        return await self.unary(self.stub.ReadThreadEntities, request)

    async def watch_session_summaries(self) -> AsyncGenerator[None]:
        """Yield once per change notice until planned renewal, which returns; anything else raises."""
        async for _ in self._watch(history_service_pb2.WatchSessionSummariesRequest()):
            yield

    async def watch_session_openings(self, *, after_position: int) -> AsyncGenerator[SessionOpened]:
        """Yield each Session recorded after `after_position`, in recording order, until planned renewal,
        which returns; anything else raises."""
        request = history_service_pb2.WatchSessionSummariesRequest(opened_after=after_position)
        async for message in self._watch(request):
            if message.HasField("session_opened"):
                yield SessionOpened(
                    session_id=UUID(message.session_opened.session_id), position=message.session_opened.position
                )

    async def _watch(
        self, request: history_service_pb2.WatchSessionSummariesRequest
    ) -> AsyncIterator[history_service_pb2.WatchSessionSummariesResponse]:
        # A long-lived stream: no call deadline. The server's opening notice bounds the handshake,
        # and its lease ends the stream with reconnect_required.
        call = self.stub.WatchSessionSummaries(request, metadata=await bearer(self.token_file))
        handshake: float | None = self.request_timeout_s
        try:
            while True:
                try:
                    async with asyncio.timeout(handshake):
                        message = await call.read()
                except grpc.aio.AioRpcError as error:
                    raise_status(error)
                handshake = None
                if message is grpc.aio.EOF:
                    raise ConnectionError("History Service ended the summary watch without planned renewal")
                assert isinstance(message, history_service_pb2.WatchSessionSummariesResponse)
                if message.HasField("reconnect_required"):
                    return
                yield message
        finally:
            call.cancel()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.redial()

    async def redial(self) -> None:
        """Close the open channel; the next call dials a fresh one."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
