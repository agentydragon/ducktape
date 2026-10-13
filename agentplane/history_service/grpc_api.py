"""Authenticated gRPC reads of retained Session history."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc
from google.protobuf.empty_pb2 import Empty
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.changes import ChangeSignal
from agentplane.grpc_servicer import Abort, ServicerResources, Status, status_mapping
from agentplane.history_service.evidence import EvidenceNotFoundError, EvidenceReader
from agentplane.history_service.storage.store import HistoryNotFoundError, Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.thread_reads import ThreadReader, ThreadScopeResetError
from agentplane.proto import history_service_pb2, history_service_pb2_grpc

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)

_OPENINGS_PAGE = 500


@dataclass(frozen=True)
class Resources(ServicerResources):
    # `caller_accounts` read transcripts: a public Session UUID is not itself authority.
    history: Store
    summaries: SummaryReader
    threads: ThreadReader
    evidence: EvidenceReader
    request_timeout_s: float
    summary_changes: ChangeSignal
    session_openings: ChangeSignal
    # How long one WatchSessionSummaries stream lasts before it asks the caller to reconnect.
    watch_lease_s: float


def _invalid_stored_entity(error: ValidationError) -> Status:
    # Stored rows outside the published view contract are a server fault, not a bad request.
    logger.error("stored thread entity failed validation", exc_info=error)
    return grpc.StatusCode.INTERNAL, "stored thread entity is invalid"


# No bearer or database detail reaches clients.
errors = status_mapping(
    Abort(HistoryNotFoundError, (grpc.StatusCode.NOT_FOUND, "session history not found")),
    Abort(EvidenceNotFoundError, (grpc.StatusCode.NOT_FOUND, "entity evidence not found")),
    Abort(ThreadScopeResetError, (grpc.StatusCode.FAILED_PRECONDITION, "thread projection epoch was reset")),
    Abort(ValidationError, _invalid_stored_entity),
    Abort(ValueError, (grpc.StatusCode.INVALID_ARGUMENT, "invalid session history request")),
    deadline="history read deadline expired",
    unavailable="history or authentication unavailable",
    also_unavailable=(SQLAlchemyError,),
)


class HistoryService(history_service_pb2_grpc.HistoryServiceServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @asynccontextmanager
    async def request(self, context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            yield

    @override
    async def ReadSessionEvents(
        self, request: history_service_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.ReadSessionEventsResponse:
        async with self.request(context):
            return await self.resources.history.read_page(
                UUID(request.session_id), after_cursor=request.after_cursor, limit=request.limit
            )

    @override
    async def ReadSessionObservations(
        self, request: history_service_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.ReadSessionObservationsResponse:
        async with self.request(context):
            page = await self.resources.history.read_observations(
                UUID(request.session_id),
                before_cursor=request.before_cursor if request.HasField("before_cursor") else None,
                after_cursor=request.after_cursor if request.HasField("after_cursor") else None,
                limit=request.limit,
            )
            return history_service_pb2.ReadSessionObservationsResponse(
                last_cursor=page.last_cursor,
                observations=[
                    history_service_pb2.SessionObservation(cursor=cursor, kind=kind)
                    for cursor, kind in page.observations
                ],
                settled=[history_service_pb2.CursorRange(first=first, last=last) for first, last in page.settled],
            )

    @override
    async def GetSessionSummary(
        self, request: history_service_pb2.GetSessionSummaryRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.SessionSummary:
        async with self.request(context):
            return await self.resources.summaries.get(UUID(request.session_id))

    @override
    async def BatchGetSessionSummaries(
        self, request: history_service_pb2.BatchGetSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.BatchGetSessionSummariesResponse:
        async with self.request(context):
            summaries = await self.resources.summaries.get_many(
                {UUID(session_id) for session_id in request.session_ids}
            )
            return history_service_pb2.BatchGetSessionSummariesResponse(summaries=summaries)

    @override
    async def FindSessions(
        self, request: history_service_pb2.FindSessionsRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.FindSessionsResponse:
        async with self.request(context):
            match request.WhichOneof("sandbox"):
                case "sandbox_name":
                    sessions = await self.resources.summaries.find(
                        sandbox_name=request.sandbox_name, sandbox_name_contains=None
                    )
                case "sandbox_name_contains":
                    sessions = await self.resources.summaries.find(
                        sandbox_name=None, sandbox_name_contains=request.sandbox_name_contains
                    )
                case _:
                    raise ValueError("a Sandbox name match is required")
            return history_service_pb2.FindSessionsResponse(sessions=sessions)

    @override
    async def ReadThreadEntities(
        self, request: history_service_pb2.ReadThreadEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.ReadThreadEntitiesResponse:
        async with self.request(context):
            return await self.resources.threads.entities(request)

    @override
    async def ReadEntityEvidence(
        self, request: history_service_pb2.ReadEntityEvidenceRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.ReadEntityEvidenceResponse:
        async with self.request(context):
            return await self.resources.evidence.evidence(request)

    @override
    async def ReadNativeFrames(
        self, request: history_service_pb2.ReadNativeFramesRequest, context: grpc.aio.ServicerContext
    ) -> history_service_pb2.ReadNativeFramesResponse:
        async with self.request(context):
            return await self.resources.evidence.native_frames(request)

    @override
    async def WatchSessionSummaries(  # type: ignore[override]
        self, request: history_service_pb2.WatchSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> None:
        """Write `changed` on open and after each change, each Session recorded since if asked, then
        `reconnect_required` once the lease ends."""
        async with errors(context):
            async with asyncio.timeout(self.resources.request_timeout_s):
                await self.resources.authenticate(context)
            deadline = asyncio.get_running_loop().time() + self.resources.watch_lease_s
            opened_after = request.opened_after if request.HasField("opened_after") else None
            # Subscribed before the opening notice, so nothing committed after it goes unannounced. A
            # recorded Session also wakes a stream that asked for none: a spurious `changed` is harmless.
            changed = asyncio.Event()
            with self.resources.summary_changes.subscribe(changed), self.resources.session_openings.subscribe(changed):
                while True:
                    changed.clear()
                    async with asyncio.timeout(self.resources.request_timeout_s):
                        await context.write(history_service_pb2.WatchSessionSummariesResponse(changed=Empty()))
                    if opened_after is not None:
                        opened_after = await self._announce_openings(context, opened_after)
                    lease = asyncio.timeout_at(deadline)
                    try:
                        async with lease:
                            await changed.wait()
                    except TimeoutError:
                        if not lease.expired():
                            raise
                        break
            async with asyncio.timeout(self.resources.request_timeout_s):
                await context.write(history_service_pb2.WatchSessionSummariesResponse(reconnect_required=Empty()))

    async def _announce_openings(self, context: grpc.aio.ServicerContext, after: int) -> int:
        """Write `session_opened` for each Session recorded after `after`; return the last position written."""
        while True:
            async with asyncio.timeout(self.resources.request_timeout_s):
                page = await self.resources.history.opened_sessions(after_position=after, limit=_OPENINGS_PAGE)
            for opened in page:
                async with asyncio.timeout(self.resources.request_timeout_s):
                    await context.write(
                        history_service_pb2.WatchSessionSummariesResponse(
                            session_opened=history_service_pb2.SessionOpened(
                                session_id=str(opened.session_id), position=opened.position
                            )
                        )
                    )
                after = opened.position
            if len(page) < _OPENINGS_PAGE:
                return after


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    history_service_pb2_grpc.add_HistoryServiceServicer_to_server(HistoryService(resources), server)
