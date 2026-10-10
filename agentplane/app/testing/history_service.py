"""Controlled History Service responses at the app's gRPC boundary.

This fixture owns no service database, runner, copier, or service implementation.
Tests publish external evidence; only the app under test projects it into its DB.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import grpc

from agentplane.history_service.client import HistoryServiceClient
from agentplane.protocol import event_log_pb2
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


@dataclass
class History:
    entries: list[event_log_pb2.EventEntry] = field(default_factory=list)
    feed: protocol_pb2.SessionFeedState | None = None


class HistoryService:
    """A programmable peer, not an alternate implementation of durable ingestion."""

    def __init__(self) -> None:
        self.histories: dict[str, History] = {}
        self.requests: list[protocol_pb2.ReadSessionEventsRequest] = []
        self.failure: grpc.StatusCode | None = None

    def open(self, session_id: UUID) -> None:
        self.histories.setdefault(str(session_id), History())

    def publish(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> None:
        history = self.histories[str(session_id)]
        # Test inputs are immutable snapshots, not shared mutable protobufs.
        for entry in entries:
            if entry.cursor <= len(history.entries):
                assert history.entries[entry.cursor - 1] == entry, "conflicting scripted evidence"
                continue
            history.entries.append(event_log_pb2.EventEntry.FromString(entry.SerializeToString()))

    def set_feed(self, session_id: UUID, feed: protocol_pb2.SessionFeedState) -> None:
        self.histories[str(session_id)].feed = protocol_pb2.SessionFeedState.FromString(feed.SerializeToString())

    async def read_events(
        self, request: protocol_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionEventsResponse:
        self.requests.append(protocol_pb2.ReadSessionEventsRequest.FromString(request.SerializeToString()))
        if self.failure is not None:
            await context.abort(self.failure, "scripted history failure")
        if request.session_id not in self.histories:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown session")
        history = self.histories[request.session_id]
        response = protocol_pb2.ReadSessionEventsResponse(
            last_cursor=history.entries[-1].cursor if history.entries else 0,
            entries=[e for e in history.entries if e.cursor > request.after_cursor][: request.limit],
        )
        if history.feed is not None:
            response.feed_state.CopyFrom(history.feed)
        return response

    async def read_observations(
        self, request: protocol_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionObservationsResponse:
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
        return protocol_pb2.ReadSessionObservationsResponse(
            last_cursor=entries[-1].cursor if entries else 0,
            observations=[
                protocol_pb2.SessionObservation(cursor=e.cursor, kind=e.event.WhichOneof("observation") or "")
                for e in selected
            ],
        )

    def handler(self) -> grpc.GenericRpcHandler:
        # Register only the public methods this fixture controls. Unscripted RPCs
        # fail UNIMPLEMENTED rather than silently reaching a real backend.
        return grpc.method_handlers_generic_handler(
            "ducktape.agentplane.history.v1.HistoryService",
            {
                "ReadSessionEvents": grpc.unary_unary_rpc_method_handler(
                    self.read_events,
                    request_deserializer=protocol_pb2.ReadSessionEventsRequest.FromString,
                    response_serializer=protocol_pb2.ReadSessionEventsResponse.SerializeToString,
                ),
                "ReadSessionObservations": grpc.unary_unary_rpc_method_handler(
                    self.read_observations,
                    request_deserializer=protocol_pb2.ReadSessionObservationsRequest.FromString,
                    response_serializer=protocol_pb2.ReadSessionObservationsResponse.SerializeToString,
                ),
            },
        )

    @asynccontextmanager
    async def connect(self, token_file: Path) -> AsyncIterator[HistoryServiceClient]:
        server = grpc.aio.server()
        server.add_generic_rpc_handlers([self.handler()])
        port = server.add_insecure_port("127.0.0.1:0")
        await asyncio.to_thread(token_file.write_text, "test-app-history-peer")
        client = HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=10)
        await server.start()
        try:
            yield client
        finally:
            await client.close()
            await server.stop(0)
