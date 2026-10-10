"""Authenticated gRPC reads of retained Session history; the Sandbox Service still writes it."""

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
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges
from agentplane.history_service.thread_reads import PayloadNotFoundError, ThreadReader, ThreadScopeResetError
from agentplane.sandbox_service.session_history.history_changes import HistoryChanges
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError, Store
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.bearer import parse_bearer, sole_header
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
    history_changes: HistoryChanges
    summary_changes: SummaryChanges
    # How long one WatchSessionSummaries stream lasts before it asks the caller to reconnect.
    watch_lease_s: float

    def __post_init__(self) -> None:
        if self.request_timeout_s <= 0 or self.watch_lease_s <= 0:
            raise ValueError("request timeout and watch lease must be positive")
        if not self.reader_accounts:
            raise ValueError("at least one history reader is required")


class HistoryService(protocol_pb2_grpc.HistoryServiceServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @asynccontextmanager
    async def reader(self, context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
        """Authenticate a reader and sanitize failures; no bearer or database detail reaches clients."""
        try:
            async with asyncio.timeout(self.resources.request_timeout_s):
                values = [value for key, value in (context.invocation_metadata() or ()) if key == "authorization"]
                value = sole_header([value for value in values if isinstance(value, str)])
                token = parse_bearer(value) if value is not None else None
                if token is None:
                    raise WorkloadPrincipalRejectedError("invalid bearer metadata")
                principal = await self.resources.principals.resolve_workload(token)
                if principal.account not in self.resources.reader_accounts:
                    await context.abort(grpc.StatusCode.PERMISSION_DENIED, "session history reader not allowed")
                yield
        except WorkloadPrincipalRejectedError:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
        except HistoryNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "session history not found")
        except PayloadNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "payload revision not found")
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

    @override
    async def ReadSessionEvents(
        self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionEventsResponse:
        async with self.reader(context):
            return await self.resources.history.read_page(
                UUID(request.session_id), after_cursor=request.after_cursor, limit=request.limit
            )

    @override
    async def ReadSessionObservations(
        self, request: history_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadSessionObservationsResponse:
        async with self.reader(context):
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
    async def ListSessionSummaries(
        self, request: history_pb2.ListSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ListSessionSummariesResponse:
        async with self.reader(context):
            return await self.resources.summaries.list_page(request)

    @override
    async def GetSessionSummary(
        self, request: history_pb2.GetSessionSummaryRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.SessionSummary:
        async with self.reader(context):
            return await self.resources.summaries.get(UUID(request.session_id))

    @override
    async def BatchGetSessionSummaries(
        self, request: history_pb2.BatchGetSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.BatchGetSessionSummariesResponse:
        async with self.reader(context):
            summaries = await self.resources.summaries.get_many({UUID(session_id) for session_id in request.session_ids})
            return history_pb2.BatchGetSessionSummariesResponse(summaries=summaries)

    @override
    async def ReadThreadEntities(
        self, request: history_pb2.ReadThreadEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadThreadEntitiesResponse:
        async with self.reader(context):
            return await self.resources.threads.entities(request)

    @override
    async def ReadPayload(
        self, request: history_pb2.ReadPayloadRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadPayloadResponse:
        async with self.reader(context):
            return await self.resources.threads.payload(request)

    @override
    async def ReadEntityEvidence(
        self, request: history_pb2.ReadEntityEvidenceRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadEntityEvidenceResponse:
        async with self.reader(context):
            return await self.resources.evidence.evidence(request)

    @override
    async def ReadNativeFrames(
        self, request: history_pb2.ReadNativeFramesRequest, context: grpc.aio.ServicerContext
    ) -> history_pb2.ReadNativeFramesResponse:
        async with self.reader(context):
            return await self.resources.evidence.native_frames(request)

    @override
    async def WatchSessionHistory(  # type: ignore[override]
        self, request: history_pb2.WatchSessionHistoryRequest, context: grpc.aio.ServicerContext
    ) -> None:
        async with self.reader(context):
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.resources.watch_lease_s
        with self.resources.history_changes.subscribe() as changed:
            while True:
                changed.clear()
                async with asyncio.timeout(self.resources.request_timeout_s):
                    await context.write(history_pb2.WatchSessionHistoryResponse(changed=Empty()))
                lease = asyncio.timeout_at(deadline)
                try:
                    async with lease:
                        await changed.wait()
                except TimeoutError:
                    if not lease.expired():
                        raise
                    break
        async with asyncio.timeout(self.resources.request_timeout_s):
            await context.write(history_pb2.WatchSessionHistoryResponse(reconnect_required=Empty()))

    @override
    async def WatchSessionSummaries(  # type: ignore[override]
        self, request: history_pb2.WatchSessionSummariesRequest, context: grpc.aio.ServicerContext
    ) -> None:
        async with self.reader(context):
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.resources.watch_lease_s
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
