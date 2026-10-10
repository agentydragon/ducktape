"""Authenticated gRPC reads of retained Session history; the Sandbox Service still writes it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.history_service import protocol_pb2_grpc
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError, Store
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.bearer import parse_bearer, sole_header
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@dataclass(frozen=True)
class Resources:
    principals: WorkloadPrincipalResolver
    history: Store
    # A public Session UUID is not itself authority: only these accounts read transcripts.
    reader_accounts: frozenset[ServiceAccountRef]
    request_timeout_s: float

    def __post_init__(self) -> None:
        if self.request_timeout_s <= 0:
            raise ValueError("request timeout must be positive")
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
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid session history request")
        except TimeoutError:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "history read deadline expired")
        except ConnectionError, SQLAlchemyError, k8s_client.ApiException:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "history or authentication unavailable")

    @override
    async def ReadSessionEvents(
        self, request: protocol_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionEventsResponse:
        async with self.reader(context):
            return await self.resources.history.read_page(
                UUID(request.session_id), after_cursor=request.after_cursor, limit=request.limit
            )

    @override
    async def ReadSessionObservations(
        self, request: protocol_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionObservationsResponse:
        async with self.reader(context):
            last_cursor, observations = await self.resources.history.read_observations(
                UUID(request.session_id),
                before_cursor=request.before_cursor if request.HasField("before_cursor") else None,
                after_cursor=request.after_cursor if request.HasField("after_cursor") else None,
                limit=request.limit,
            )
            return protocol_pb2.ReadSessionObservationsResponse(
                last_cursor=last_cursor,
                observations=[
                    protocol_pb2.SessionObservation(cursor=cursor, kind=kind) for cursor, kind in observations
                ],
            )


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    protocol_pb2_grpc.add_HistoryServiceServicer_to_server(HistoryService(resources), server)
