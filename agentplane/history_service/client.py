"""gRPC client for History Service reads, with no Kubernetes or Sandbox Service fallback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from types import TracebackType
from typing import Never, Self

import grpc

from agentplane.grpc_options import LARGE_EVENT_RECEIVE_OPTIONS
from agentplane.proto import history_service_pb2, history_service_pb2_grpc

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class HistoryServiceError(ConnectionError):
    def __init__(self, code: grpc.StatusCode, details: str | None) -> None:
        super().__init__(f"{code.name}: {details}")
        self.code = code


def _raise(error: grpc.aio.AioRpcError) -> Never:
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError(f"{error.code().name}: {error.details()}") from error
    raise HistoryServiceError(error.code(), error.details()) from error


class HistoryServiceClient:
    def __init__(self, target: str, *, token_file: Path, request_timeout_s: float) -> None:
        if request_timeout_s <= 0:
            raise ValueError("request timeout must be positive")
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

    async def metadata(self) -> tuple[tuple[str, str], ...]:
        # Projected tokens rotate. Read for every RPC.
        token = (await asyncio.to_thread(self.token_file.read_text)).strip()
        return (("authorization", f"Bearer {token}"),)

    def new_client(self) -> HistoryServiceClient:
        """Create an independent channel with this client's endpoint and credentials."""
        return HistoryServiceClient(self.target, token_file=self.token_file, request_timeout_s=self.request_timeout_s)

    async def read_session_events(
        self, session_id: str, *, after_cursor: int = 0, limit: int = 128
    ) -> history_service_pb2.ReadSessionEventsResponse:
        if not session_id or after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history page")
        try:
            return await self.stub.ReadSessionEvents(
                history_service_pb2.ReadSessionEventsRequest(
                    session_id=session_id, after_cursor=after_cursor, limit=limit
                ),
                metadata=await self.metadata(),
                timeout=self.request_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def read_session_observations(
        self, session_id: str, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> history_service_pb2.ReadSessionObservationsResponse:
        if (
            not session_id
            or not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid session observation page")
        request = history_service_pb2.ReadSessionObservationsRequest(session_id=session_id, limit=limit)
        if before_cursor is not None:
            request.before_cursor = before_cursor
        if after_cursor is not None:
            request.after_cursor = after_cursor
        try:
            return await self.stub.ReadSessionObservations(
                request, metadata=await self.metadata(), timeout=self.request_timeout_s
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def get_session_summary(self, session_id: str) -> history_service_pb2.SessionSummary:
        try:
            return await self.stub.GetSessionSummary(
                history_service_pb2.GetSessionSummaryRequest(session_id=session_id),
                metadata=await self.metadata(),
                timeout=self.request_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def batch_get_session_summaries(
        self, session_ids: Collection[str]
    ) -> list[history_service_pb2.SessionSummary]:
        try:
            response = await self.stub.BatchGetSessionSummaries(
                history_service_pb2.BatchGetSessionSummariesRequest(session_ids=session_ids),
                metadata=await self.metadata(),
                timeout=self.request_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)
        return list(response.summaries)

    async def read_thread_entities(
        self, request: history_service_pb2.ReadThreadEntitiesRequest
    ) -> history_service_pb2.ReadThreadEntitiesResponse:
        try:
            return await self.stub.ReadThreadEntities(
                request, metadata=await self.metadata(), timeout=self.request_timeout_s
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def read_entity_evidence(
        self, request: history_service_pb2.ReadEntityEvidenceRequest
    ) -> history_service_pb2.ReadEntityEvidenceResponse:
        try:
            return await self.stub.ReadEntityEvidence(
                request, metadata=await self.metadata(), timeout=self.request_timeout_s
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def read_native_frames(
        self, request: history_service_pb2.ReadNativeFramesRequest
    ) -> history_service_pb2.ReadNativeFramesResponse:
        try:
            return await self.stub.ReadNativeFrames(
                request, metadata=await self.metadata(), timeout=self.request_timeout_s
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def watch_session_summaries(self) -> AsyncIterator[None]:
        """Yield once per change notice until planned renewal, which returns; anything else raises."""
        # A long-lived stream: no call deadline. The server's opening notice bounds the handshake,
        # and its lease ends the stream with reconnect_required.
        call = self.stub.WatchSessionSummaries(
            history_service_pb2.WatchSessionSummariesRequest(), metadata=await self.metadata()
        )
        handshake: float | None = self.request_timeout_s
        try:
            while True:
                try:
                    async with asyncio.timeout(handshake):
                        message = await call.read()
                except grpc.aio.AioRpcError as error:
                    _raise(error)
                handshake = None
                if message is grpc.aio.EOF:
                    raise ConnectionError("History Service ended the summary watch without planned renewal")
                assert isinstance(message, history_service_pb2.WatchSessionSummariesResponse)
                if message.HasField("reconnect_required"):
                    return
                yield
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
