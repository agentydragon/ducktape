"""A thread's stored event log as server-sent events: the attachment snapshot, every entry after a
cursor, then each entry as it commits, until the feed ends.

It reads only the database, so any replica serves it and no runner has to be reachable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.changes import Changes
from agentplane.app.thread.event_log import EventLogStore, FeedEnd, FeedError, ThreadNotFoundError

# gazelle:include_dep @pypi//protobuf

REPLAY_PAGE = 1000
KEEPALIVE_S = 15


async def follow(
    event_logs: EventLogStore, changes: Changes, thread_id: UUID, *, after_cursor: int
) -> AsyncGenerator[bytes]:
    """Follow the committed archive without requiring or starting a runner attachment."""
    if await event_logs.runner_session(thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    waiter = asyncio.Event()
    cursor = after_cursor
    with changes.subscribe(waiter):
        attached_sent = False
        while True:
            waiter.clear()
            snapshot = await event_logs.feed_state(thread_id)
            if not attached_sent and snapshot is not None:
                yield _frame("attached", MessageToDict(snapshot.attached))
                attached_sent = True
            while page := await event_logs.events(thread_id, after_cursor=cursor, limit=REPLAY_PAGE):
                for entry in page:
                    yield _frame("event", MessageToDict(entry), event_id=entry.cursor)
                    cursor = entry.cursor
            snapshot = await event_logs.feed_state(thread_id)
            if snapshot is not None and snapshot.end is not None:
                if await event_logs.last_cursor(thread_id) > cursor:
                    continue
                match snapshot.end:
                    case FeedEnd():
                        yield _frame("end", {})
                    case FeedError(message=message):
                        yield _frame("error", {"message": message})
                return
            try:
                await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield b": keepalive\n\n"


def _frame(event: str, data: dict[str, object], *, event_id: int | None = None) -> bytes:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data)}")
    return ("\n".join(lines) + "\n\n").encode()
