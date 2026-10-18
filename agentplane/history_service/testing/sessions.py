"""The writer tests bind pushed Sessions to, and Sessions it has pushed."""

from uuid import UUID, uuid4

from more_itertools import consecutive_groups
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.storage.db import SessionEvent, SessionEventRemovedRange
from agentplane.history_service.storage.store import Store, Writer
from agentplane.proto import event_log_pb2

# gazelle:include_dep @pypi//protobuf

WRITER = Writer(namespace="test-sandbox-namespace", name="test-sandbox-runner", uid="test-runner-account-uid")


async def retained_session(history: Store, entries: list[event_log_pb2.EventEntry]) -> UUID:
    """A new Session bound to `WRITER` whose committed history is `entries`."""
    session_id = uuid4()
    await history.bind_writer(session_id, WRITER)
    await history.append(session_id, entries)
    return session_id


async def remove_entries(engine: AsyncEngine, session_id: UUID, cursors: set[int]) -> None:
    """Leave the Session's stored history as a cleanup migration does: the entries deleted, their cursors removed."""
    async with engine.begin() as connection:
        await connection.execute(
            delete(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.cursor.in_(cursors))
        )
        await connection.execute(
            insert(SessionEventRemovedRange),
            [
                {"session_id": session_id, "first_cursor": run[0], "last_cursor": run[-1]}
                for run in map(list, consecutive_groups(sorted(cursors)))
            ],
        )
