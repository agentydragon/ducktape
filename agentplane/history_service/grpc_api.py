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
from kubernetes_asyncio import client as k8s_client
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.history_service import protocol_pb2 as history_pb2, protocol_pb2_grpc
from agentplane.history_service.evidence import EvidenceNotFoundError, EvidenceReader
from agentplane.history_service.storage.store import HistoryNotFoundError, Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges
from agentplane.history_service.thread_reads import ThreadReader, ThreadScopeResetError
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resources:
    principals: WorkloadPrincipalResolver
    history: Store
    summaries: SummaryReader
    threads: ThreadReader
    evidence: EvidenceReader
    # A public Session UUID is not itself authority: only these accounts read transcripts.
    reader_accounts: frozenset[ServiceAccountRef]
    request_timeout_s: float
    summary_changes: SummaryChanges
    # How long one WatchSessionSummaries stream lasts before it asks the caller to reconnect.
    watch_lease_s: float

    def __post_init__(self) -> None:
        if self.request_timeout_s <= 0 or self.watch_lease_s <= 0:
            raise ValueError("request timeout and watch lease must be positive")
        if not self.reader_accounts:
            raise ValueError("at least one history reader is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> None:
        await authenticate(self.principals, self.reader_accounts, context)


@asynccontextmanager
async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Sanitize failures; no bearer or database detail reaches clients."""
    try:
        yield
    except WorkloadPrincipalRejectedError:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
    except HistoryNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "session history not found")
    except EvidenceNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "entity evidence not found")
    except ThreadScopeResetError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "thread projection epoch was reset")
    except ValidationError:
        # Stored rows outside the published view contract are a server fault, not a bad request.
        logger.exception("stored thread entity failed validation")
        await context.abort(grpc.StatusCode.INTERNAL, "stored thread entity is invalid")
    except ValueError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid session history request")
    except TimeoutError:
        await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "history read deadline expired")
    except ConnectionError, SQLAlchemyError, k8s_client.ApiException:
        await context.abort(grpc.StatusCode.UNAVAILABLE, "history or authentication unavailable")


class HistoryService(protocol_pb2_grpc.HistoryServiceServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @override
    async def ReadSessionEvents(
        self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionEventsResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            return await self.resources.history.read_page(
                UUID(request.session_id), after_cursor=request.after_cursor, limit=request.limit
            )

    @override
    async def ReadSessionObservations(
        self, request: history_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionObservationsResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            page = await self.resources.history.read_observations(
                UUID(request.session_id),
                before_cursor=request.before_cursor if request.HasField("before_cursor") else None,
                after_cursor=request.after_cursor if request.HasField("after_cursor") else None,
                limit=request.limit,
            )
            return history_pb2.ReadSessionObservationsResponse(
                last_cursor=page.last_cursor,
                observations=[
                    history_pb2.SessionObservation(cursor=cursor, kind=kind) for cursor, kind in page.observations
                ],
                settled=[history_pb2.CursorRange(first=first, last=last) for first, last in page.settled],
            )

    @override
    async def GetSessionSummary(
        self, request: history_pb2.GetSessionSummaryRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.SessionSummary:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            return await self.resources.summaries.get(UUID(request.session_id))

    @override
    async def BatchGetSessionSummaries(
        self, request: history_pb2.BatchGetSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.BatchGetSessionSummariesResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            summaries = await self.resources.summaries.get_many(
                {UUID(session_id) for session_id in request.session_ids}
            )
            return history_pb2.BatchGetSessionSummariesResponse(summaries=summaries)

    @override
    async def ReadThreadEntities(
        self, request: history_pb2.ReadThreadEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadThreadEntitiesResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            return await self.resources.threads.entities(request)

    @override
    async def ReadEntityEvidence(
        self, request: history_pb2.ReadEntityEvidenceRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadEntityEvidenceResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            return await self.resources.evidence.evidence(request)

    @override
    async def ReadNativeFrames(
        self, request: history_pb2.ReadNativeFramesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadNativeFramesResponse:
        async with errors(context), asyncio.timeout(self.resources.request_timeout_s):
            await self.resources.authenticate(context)
            return await self.resources.evidence.native_frames(request)

    @override
    async def WatchSessionSummaries(  # type: ignore[override]
        self, request: history_pb2.WatchSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> None:
        """Write `changed` on open and after each change, then `reconnect_required` once the lease ends."""
        async with errors(context):
            async with asyncio.timeout(self.resources.request_timeout_s):
                await self.resources.authenticate(context)
            deadline = asyncio.get_running_loop().time() + self.resources.watch_lease_s
            # Subscribed before the opening notice, so nothing committed after it goes unannounced.
            with self.resources.summary_changes.subscribe() as changed:
                while True:
                    changed.clear()
                    async with asyncio.timeout(self.resources.request_timeout_s):
                        await context.write(history_pb2.WatchSessionSummariesResponse(changed=Empty()))
                    lease = asyncio.timeout_at(deadline)
                    try:
                        async with lease:
                            await changed.wait()
                    except TimeoutError:
                        if not lease.expired():
                            raise
                        break
            async with asyncio.timeout(self.resources.request_timeout_s):
                await context.write(history_pb2.WatchSessionSummariesResponse(reconnect_required=Empty()))


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    protocol_pb2_grpc.add_HistoryServiceServicer_to_server(HistoryService(resources), server)
