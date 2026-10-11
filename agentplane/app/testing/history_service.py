"""Controlled History Service responses at the app's gRPC boundary.

This fixture owns no service database, runner, copier, or service implementation.
Tests publish external evidence; only the app under test projects it into its DB.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import override
from uuid import UUID

import grpc
from google.protobuf.empty_pb2 import Empty

from agentplane.grpc_process import serving
from agentplane.history_service import protocol_pb2 as history_pb2, protocol_pb2_grpc
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2

# gazelle:include_dep @pypi//protobuf


@dataclass
class History:
    entries: list[event_log_pb2.EventEntry] = field(default_factory=list)
    feed: history_pb2.SessionFeedState | None = None


class HistoryService(protocol_pb2_grpc.HistoryServiceServicer):
    """A programmable peer, not an alternate implementation of durable ingestion. RPCs it does not
    script answer UNIMPLEMENTED rather than silently reaching a real backend."""

    def __init__(self) -> None:
        self.histories: dict[str, History] = {}
        self.requests: list[history_pb2.ReadSessionEventsRequest] = []
        self.failure: grpc.StatusCode | None = None
        self._history_waiters: set[asyncio.Event] = set()
        self.history_watch_opened = asyncio.Event()

    def open(self, session_id: UUID) -> None:
        if str(session_id) not in self.histories:
            self.histories[str(session_id)] = History()
            self._wake_history()

    def publish(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> None:
        history = self.histories[str(session_id)]
        changed = False
        # Test inputs are immutable snapshots, not shared mutable protobufs.
        for entry in entries:
            if entry.cursor <= len(history.entries):
                assert history.entries[entry.cursor - 1] == entry, "conflicting scripted evidence"
                continue
            history.entries.append(event_log_pb2.EventEntry.FromString(entry.SerializeToString()))
            changed = True
        if changed:
            self._wake_history()

    def set_feed(self, session_id: UUID, feed: history_pb2.SessionFeedState) -> None:
        snapshot = history_pb2.SessionFeedState.FromString(feed.SerializeToString())
        history = self.histories[str(session_id)]
        if history.feed is None or history.feed != snapshot:
            history.feed = snapshot
            self._wake_history()

    def _wake_history(self) -> None:
        for changed in self._history_waiters:
            changed.set()

    @override
    async def ReadSessionEvents(
        self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionEventsResponse:
        self.requests.append(history_pb2.ReadSessionEventsRequest.FromString(request.SerializeToString()))
        if self.failure is not None:
            await context.abort(self.failure, "scripted history failure")
        if request.session_id not in self.histories:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown session")
        history = self.histories[request.session_id]
        response = history_pb2.ReadSessionEventsResponse(
            last_cursor=history.entries[-1].cursor if history.entries else 0,
            entries=[e for e in history.entries if e.cursor > request.after_cursor][: request.limit],
        )
        if history.feed is not None:
            response.feed_state.CopyFrom(history.feed)
        return response

    @override
    async def ReadSessionObservations(
        self, request: history_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionObservationsResponse:
        if self.failure is not None:
            await context.abort(self.failure, "scripted history failure")
        if request.session_id not in self.histories:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown session")
        entries = self.histories[request.session_id].entries
        if request.HasField("after_cursor"):
            selected = [e for e in entries if e.cursor > request.after_cursor][: request.limit]
        else:
            selected = [
                e for e in entries if not request.HasField("before_cursor") or e.cursor < request.before_cursor
            ][-request.limit :]
        return history_pb2.ReadSessionObservationsResponse(
            last_cursor=entries[-1].cursor if entries else 0,
            observations=[
                history_pb2.SessionObservation(cursor=e.cursor, kind=e.event.WhichOneof("observation") or "")
                for e in selected
            ],
        )

    @override
    async def WatchSessionHistory(
        self, request: history_pb2.WatchSessionHistoryRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[history_pb2.WatchSessionHistoryResponse]:
        changed = asyncio.Event()
        self._history_waiters.add(changed)
        try:
            self.history_watch_opened.set()
            while True:
                changed.clear()
                yield history_pb2.WatchSessionHistoryResponse(changed=Empty())
                await changed.wait()
        finally:
            self._history_waiters.discard(changed)

    @asynccontextmanager
    async def connect(self, token_file: Path) -> AsyncIterator[HistoryServiceClient]:
        server = grpc.aio.server()
        protocol_pb2_grpc.add_HistoryServiceServicer_to_server(self, server)
        await asyncio.to_thread(token_file.write_text, "test-app-history-peer")
        async with (
            serving(server, "127.0.0.1:0", grace_s=0) as port,
            HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=10) as client,
        ):
            yield client
