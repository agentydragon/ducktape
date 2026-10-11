"""A History Service servicer whose every RPC answers UNIMPLEMENTED."""

from collections.abc import AsyncIterator
from typing import override

import grpc

from agentplane.history_service import protocol_pb2 as history_pb2, protocol_pb2_grpc

# gazelle:include_dep @pypi//protobuf


class UnimplementedHistoryService(protocol_pb2_grpc.HistoryServiceServicer):
    """The generated stub marks each RPC abstract, so a fake subclasses this and overrides only the
    RPCs its test drives."""

    @override
    async def ReadSessionEvents(
        self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionEventsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ReadSessionEvents")

    @override
    async def ReadSessionObservations(
        self, request: history_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionObservationsResponse:
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ReadSessionObservations"
        )

    @override
    async def ListSessionSummaries(
        self, request: history_pb2.ListSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ListSessionSummariesResponse:
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ListSessionSummaries"
        )

    @override
    async def GetSessionSummary(
        self, request: history_pb2.GetSessionSummaryRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.SessionSummary:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve GetSessionSummary")

    @override
    async def BatchGetSessionSummaries(
        self, request: history_pb2.BatchGetSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.BatchGetSessionSummariesResponse:
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve BatchGetSessionSummaries"
        )

    @override
    async def ReadThreadEntities(
        self, request: history_pb2.ReadThreadEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadThreadEntitiesResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ReadThreadEntities")

    @override
    async def ReadEntityEvidence(
        self, request: history_pb2.ReadEntityEvidenceRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadEntityEvidenceResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ReadEntityEvidence")

    @override
    async def ReadNativeFrames(
        self, request: history_pb2.ReadNativeFramesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadNativeFramesResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve ReadNativeFrames")

    @override
    async def WatchSessionHistory(
        self, request: history_pb2.WatchSessionHistoryRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[history_pb2.WatchSessionHistoryResponse]:
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve WatchSessionHistory"
        )
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    @override
    async def WatchSessionSummaries(
        self, request: history_pb2.WatchSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[history_pb2.WatchSessionSummariesResponse]:
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED, "the fake History Service does not serve WatchSessionSummaries"
        )
        yield  # Unreachable; makes this the streaming handler gRPC calls.
