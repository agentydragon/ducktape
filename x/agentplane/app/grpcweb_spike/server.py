"""The same ThreadEvents stream, served as native gRPC for an Envoy gRPC-Web translation.

Measured against the Connect mount in x/agentplane/app/thread_events.py, which serves the identical
fold in the app's own ASGI process. What differs here is everything around the fold: a second server
on a second port, an error vocabulary of its own, and an authorization path that cannot reach
FastAPI's dependency system.

Spike. Nothing imports this outside its own test; findings are in README.md.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import grpc
from connecpy.code import Code
from connecpy.exceptions import ConnecpyException

from x.agentplane.app import thread_events_pb2, thread_events_pb2_grpc
from x.agentplane.app.shutdown import Drain
from x.agentplane.app.thread_events import ThreadEventsService
from x.agentplane.app.trajectory import TrajectoryStore

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Finding 1: the fold is transport-neutral but its errors are not. `ThreadEventsService.frames`
# raises `ConnecpyException`, which is Connect's vocabulary; gRPC wants a `StatusCode`. Either the
# fold mints a third, transport-independent error type that both adapters translate, or one
# transport's exception type leaks into the other -- which is what this table is.
_CODES = {Code.NOT_FOUND: grpc.StatusCode.NOT_FOUND, Code.INVALID_ARGUMENT: grpc.StatusCode.INVALID_ARGUMENT}


class ThreadEventsServicer(thread_events_pb2_grpc.ThreadEventsServicer):
    def __init__(self, store: TrajectoryStore, drain: Drain) -> None:
        self._service = ThreadEventsService(store, drain)
        self._drain = drain

    async def FollowEvents(  # noqa: N802 - the generated servicer's method name
        self, request: thread_events_pb2.FollowEventsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[thread_events_pb2.FollowEventsResponse]:
        try:
            async for frame in self._drain.until(self._service.frames(request)):
                yield frame
        except ConnecpyException as refused:
            await context.abort(_CODES.get(refused.code, grpc.StatusCode.UNKNOWN), refused.message)


async def serve(store: TrajectoryStore, drain: Drain, *, address: str = "127.0.0.1:0") -> tuple[grpc.aio.Server, int]:
    """Finding 2: this is a second server. It shares the store object in-process, but it is its own
    listener on its own port, which needs its own Service port, NetworkPolicy and readiness."""
    server = grpc.aio.server()
    thread_events_pb2_grpc.add_ThreadEventsServicer_to_server(ThreadEventsServicer(store, drain), server)
    port = server.add_insecure_port(address)
    await server.start()
    return server, port
