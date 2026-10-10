"""gRPC client for History Service reads, with no Kubernetes or Sandbox Service fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Never

import grpc

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.history_service import protocol_pb2_grpc
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class HistoryServiceError(ConnectionError):
    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(f"History Service returned {code.name}")
        self.code = code


def _raise(error: grpc.aio.AioRpcError) -> Never:
    # Do not include upstream details: they may contain credentials or event payloads.
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError("History Service read deadline expired") from error
    raise HistoryServiceError(error.code()) from error


class HistoryServiceClient:
    def __init__(
        self,
        target: str,
        *,
        token_file: Path,
        request_timeout_s: float,
        channel_options: Mapping[str, int | str] | None = None,
    ) -> None:
        if request_timeout_s <= 0:
            raise ValueError("request timeout must be positive")
        self.target = target
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self._channel_options = channel_options
        self._channel: grpc.aio.Channel | None = None
        self._stub: protocol_pb2_grpc.HistoryServiceAsyncStub | None = None

    @property
    def stub(self) -> protocol_pb2_grpc.HistoryServiceAsyncStub:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                self.target, options=grpc_channel_option_kvps(self._channel_options)
            )
            self._stub = protocol_pb2_grpc.HistoryServiceStub(self._channel)
        return self._stub

    async def metadata(self) -> tuple[tuple[str, str], ...]:
        # Projected tokens rotate. Read for every RPC.
        token = (await asyncio.to_thread(self.token_file.read_text)).strip()
        return (("authorization", f"Bearer {token}"),)

    async def read_session_events(
        self, session_id: str, *, after_cursor: int = 0, limit: int = 128
    ) -> protocol_pb2.ReadSessionEventsResponse:
        if not session_id or after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history page")
        try:
            return await self.stub.ReadSessionEvents(
                protocol_pb2.ReadSessionEventsRequest(session_id=session_id, after_cursor=after_cursor, limit=limit),
                metadata=await self.metadata(),
                timeout=self.request_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def read_session_observations(
        self, session_id: str, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> protocol_pb2.ReadSessionObservationsResponse:
        if (
            not session_id
            or not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid session observation page")
        request = protocol_pb2.ReadSessionObservationsRequest(session_id=session_id, limit=limit)
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

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
