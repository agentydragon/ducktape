"""The Thread Event stream a browser follows, served from the archive alone.

No runner attachment is opened or required: a stream reads what this app has already committed, and
the separate ingestion feed is what puts entries there. So any replica can serve any Thread, and a
reader outliving the runner still reaches the whole archived prefix.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from connecpy.code import Code
from connecpy.exceptions import ConnecpyException
from connecpy.request import RequestContext

from x.agentplane.app import thread_events_pb2
from x.agentplane.app.connect import RPC_PREFIX
from x.agentplane.app.shutdown import Drain
from x.agentplane.app.trajectory import FeedEnd, FeedError, TrajectoryStore

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

REPLAY_PAGE = 1000
HEARTBEAT_S = 15
# Where a browser posts to follow a Thread: the shared mount prefix, then the path connecpy routes
# on, which is the service's fully qualified name and the method. Shared rather than spelled out
# again, because a test matching on this path has to match the one the app actually serves.
FOLLOW_EVENTS_PATH = f"{RPC_PREFIX}/ducktape.agentplane.app.v1.ThreadEvents/FollowEvents"


class ThreadEventsService:
    """Implements the generated `ThreadEvents` protocol over the archive.

    The drain is not a detail of one route: a followed stream waits out a quiet Thread for as long
    as the reader keeps it open, and shutdown has to end it rather than spend the graceful budget
    waiting for a frame that is not coming.
    """

    def __init__(self, store: TrajectoryStore, drain: Drain) -> None:
        self._store = store
        self._drain = drain

    def follow_events(
        self, request: thread_events_pb2.FollowEventsRequest, ctx: RequestContext
    ) -> AsyncIterator[thread_events_pb2.FollowEventsResponse]:
        return self._drain.until(self.frames(request))

    async def frames(
        self, request: thread_events_pb2.FollowEventsRequest
    ) -> AsyncIterator[thread_events_pb2.FollowEventsResponse]:
        """The frames a follower is owed, without the drain or a transport around them: what
        //x/agentplane/app:test_thread_events drives, so that what it asserts is this fold and
        not the library's framing."""
        thread_id = _thread_id(request.thread_id)
        thread = await self._store.get_thread(thread_id)
        if thread is None:
            raise ConnecpyException(Code.NOT_FOUND, f"no thread {thread_id}")
        if request.after_cursor > thread.last_cursor:
            raise ConnecpyException(Code.INVALID_ARGUMENT, "cursor is beyond the archived Thread prefix")
        # Connect sends no response headers until the first message, so without this a caller
        # following a quiet Thread could not tell an open stream from a stalled request.
        yield thread_events_pb2.FollowEventsResponse(heartbeat=thread_events_pb2.Heartbeat())
        cursor = request.after_cursor
        waiter = asyncio.Event()
        with self._store.changes.subscribe(waiter):
            attached_sent = False
            while True:
                waiter.clear()
                snapshot = await self._store.feed_state(thread_id)
                if not attached_sent and snapshot is not None:
                    yield thread_events_pb2.FollowEventsResponse(attached=snapshot.attached)
                    attached_sent = True
                while page := await self._store.events(thread_id, after_cursor=cursor, limit=REPLAY_PAGE):
                    for entry in page:
                        yield thread_events_pb2.FollowEventsResponse(entry=entry)
                        cursor = entry.cursor
                snapshot = await self._store.feed_state(thread_id)
                if snapshot is not None and snapshot.end is not None:
                    # An end committed while this loop was mid-page leaves entries behind it.
                    if await self._store.last_cursor(thread_id) > cursor:
                        continue
                    match snapshot.end:
                        case FeedEnd():
                            yield thread_events_pb2.FollowEventsResponse(ended=thread_events_pb2.FeedEnded())
                        case FeedError(message=message):
                            yield thread_events_pb2.FollowEventsResponse(
                                ended=thread_events_pb2.FeedEnded(error=message)
                            )
                    return
                try:
                    async with asyncio.timeout(HEARTBEAT_S):
                        await waiter.wait()
                except TimeoutError:
                    yield thread_events_pb2.FollowEventsResponse(heartbeat=thread_events_pb2.Heartbeat())


def _thread_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise ConnecpyException(Code.INVALID_ARGUMENT, f"thread_id is not a UUID: {value!r}") from error
