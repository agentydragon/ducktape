"""A thread's stored event log as server-sent events: the attachment snapshot, every entry after a
cursor, then each entry as it commits, until the feed ends.

It reads only the database, so any replica serves it and no runner has to be reachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.sse import frame
from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.summaries import FeedEnd, FeedError
from agentplane.changes import ChangeSignal

# gazelle:include_dep @pypi//protobuf

REPLAY_PAGE = 1000
KEEPALIVE_S = 15


async def follow(
    event_logs: EventLogStore, changes: ChangeSignal, thread_id: UUID, *, after_cursor: int
) -> AsyncGenerator[bytes]:
    """Follow the committed archive without requiring or starting a runner attachment."""
    if not await event_logs.exists(thread_id):
        raise ThreadNotFoundError(thread_id)
    waiter = asyncio.Event()
    cursor = after_cursor
    with changes.subscribe(waiter):
        attached_sent = False
        while True:
            waiter.clear()
            snapshot = await event_logs.feed_state(thread_id)
            if not attached_sent and snapshot is not None:
                yield frame("attached", MessageToDict(snapshot.attached))
                attached_sent = True
            while page := await event_logs.events(thread_id, after_cursor=cursor, limit=REPLAY_PAGE):
                for entry in page:
                    yield frame("event", MessageToDict(entry), event_id=entry.cursor)
                    cursor = entry.cursor
            snapshot = await event_logs.feed_state(thread_id)
            # A terminal snapshot may precede fold catch-up, or be an older end that entries
            # already streamed have superseded and the fold has yet to clear. It ends the stream
            # only once it is at the streamed cursor; otherwise wait without busy-polling.
            if (
                snapshot is not None
                and snapshot.end is not None
                and snapshot.attached.last_cursor >= cursor
                and await event_logs.last_cursor(thread_id) <= cursor
            ):
                match snapshot.end:
                    case FeedEnd():
                        yield frame("end", {})
                    case FeedError(message=message):
                        yield frame("error", {"message": message})
                return
            try:
                await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield b": keepalive\n\n"
