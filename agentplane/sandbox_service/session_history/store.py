"""Transactionally append and replay a runner's contiguous Event prefix."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.protocol import event_log_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.commands.db import Session
from agentplane.sandbox_service.session_history.db import CHANGE_POSITION, SessionEvent, SessionHistory
from agentplane.sandbox_service.session_history.session_changes import CHANNEL

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


# The service's own database; no other advisory lock uses this key.
CHANGE_POSITION_LOCK = 0x53455343

# The Sandbox Service registry's columns this table still repeats.
REGISTRY_COLUMNS = (
    "id",
    "sandbox_namespace",
    "sandbox_name",
    "sandbox_uid",
    "runner_session_id",
    "caller_namespace",
    "caller_name",
    "open_key",
    "open_request",
    "launch_spec",
    "change_position",
)


async def next_change_position(session: AsyncSession) -> int:
    """Allocate a WatchSessions position that becomes visible in commit order, and wake watchers.

    The transaction lock is held to commit, so no position commits after a larger one: a reader
    resuming after any position it has seen misses nothing. A rolled-back position is a gap.
    """
    await session.execute(select(func.pg_advisory_xact_lock(CHANGE_POSITION_LOCK)))
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(CHANNEL, "")))
    position = await session.scalar(select(CHANGE_POSITION.next_value()))
    assert position is not None
    return position


class HistoryConflictError(ValueError):
    """An entry skips or disagrees with the already stored runner prefix."""


class HistoryNotFoundError(LookupError):
    pass


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def open(
        self,
        session_id: UUID,
        *,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID | None,
        runner_session_id: str,
    ) -> None:
        """Register a durable ID and its immutable physical locator, including legacy IDs.

        The ID belongs to the Session (and remains usable after the Sandbox is deleted).
        Re-opening the same ID with another runner is an error, not a history overwrite.
        """
        async with self._sessions.begin() as session:
            await session.execute(
                insert(SessionHistory)
                .values(
                    id=session_id,
                    sandbox_namespace=sandbox_namespace,
                    sandbox_name=sandbox_name,
                    sandbox_uid=sandbox_uid,
                    runner_session_id=runner_session_id,
                    source_id=None,
                    last_cursor=0,
                    change_position=await next_change_position(session),
                )
                .on_conflict_do_nothing()
            )
            row = await session.get(SessionHistory, session_id)
            if row is None:
                raise HistoryConflictError(f"runner locator already belongs to another session, not {session_id}")
            if (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid, row.runner_session_id) != (
                sandbox_namespace,
                sandbox_name,
                sandbox_uid,
                runner_session_id,
            ):
                raise HistoryConflictError(f"session {session_id} has a different runner locator")

    async def registry_records(self) -> list[SessionHistory]:
        """Every row, for the Sandbox Service's registry import."""
        async with self._sessions() as session:
            return list(await session.scalars(select(SessionHistory)))

    async def mirror(self, record: Session) -> None:
        """Insert the history row of a registered Session with the registry's values, or confirm it.

        The ingesters append to this row; it repeats the Sandbox Service registry until they
        create their own.
        """
        values = {column: getattr(record, column) for column in REGISTRY_COLUMNS}
        async with self._sessions.begin() as session:
            await session.execute(
                insert(SessionHistory).values(**values, source_id=None, last_cursor=0).on_conflict_do_nothing()
            )
            row = await session.get(SessionHistory, record.id)
            if row is None or any(getattr(row, column) != value for column, value in values.items()):
                raise HistoryConflictError(f"session {record.id} history row differs from its registry record")

    async def append(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> int:
        """Replay exact duplicates or extend the prefix; serialize concurrent writers by Session ID.

        Validation and the new checkpoint commit together. On any error the entire batch rolls
        back. An empty batch is a checkpoint read, not an inferred runner high-water mark.
        """
        async with self._sessions.begin() as session:
            row = await session.scalar(select(SessionHistory).where(SessionHistory.id == session_id).with_for_update())
            if row is None:
                raise HistoryNotFoundError(session_id)
            for entry in entries:
                cursor = entry.cursor
                if cursor == 0 or not entry.origin.source_id or entry.origin.sequence != cursor:
                    raise HistoryConflictError(f"invalid runner origin at {cursor}")
                if row.source_id is not None and row.source_id != entry.origin.source_id:
                    raise HistoryConflictError(f"source changed at {cursor}")
                payload = entry.SerializeToString(deterministic=True)
                if cursor <= row.last_cursor:
                    existing = await session.get(SessionEvent, (session_id, cursor))
                    if existing is None or existing.payload != payload:
                        raise HistoryConflictError(f"conflicting entry at {cursor}")
                    continue
                if cursor != row.last_cursor + 1:
                    raise HistoryConflictError(f"expected {row.last_cursor + 1}, received {cursor}")
                session.add(SessionEvent(session_id=session_id, cursor=cursor, payload=payload))
                row.source_id = entry.origin.source_id
                row.last_cursor = cursor
            return row.last_cursor

    async def record_feed_state(self, session_id: UUID, feed: protocol_pb2.SessionFeedState) -> None:
        """Record only a covered runner snapshot; delayed copiers cannot rewind it or drop a seal.

        The snapshot may name the runner session (a direct runner attach) or the public Session ID
        (a Sandbox Service follow); it is stored under the runner's, as either copier wrote it.
        """
        snapshot = protocol_pb2.SessionFeedState()
        snapshot.CopyFrom(feed)
        async with self._sessions.begin() as session:
            row = await session.scalar(select(SessionHistory).where(SessionHistory.id == session_id).with_for_update())
            if row is None:
                raise HistoryNotFoundError(session_id)
            if (
                row.runner_session_id is None
                or not snapshot.HasField("attached")
                or snapshot.attached.session_id not in (row.runner_session_id, str(row.id))
            ):
                raise HistoryConflictError("feed snapshot does not match the bound runner session")
            snapshot.attached.session_id = row.runner_session_id
            if snapshot.attached.last_cursor > row.last_cursor:
                raise HistoryConflictError("feed snapshot is ahead of committed history")
            if snapshot.HasField("sealed") and snapshot.sealed.cursor != row.last_cursor:
                raise HistoryConflictError("seal does not end the committed history")
            if row.feed_state is not None:
                prior = protocol_pb2.SessionFeedState.FromString(row.feed_state)
                if prior.HasField("sealed"):
                    if snapshot.HasField("sealed") and snapshot.sealed != prior.sealed:
                        raise HistoryConflictError("conflicting seals")
                    snapshot.sealed.CopyFrom(prior.sealed)
                if snapshot.attached.last_cursor < prior.attached.last_cursor:
                    snapshot.attached.CopyFrom(prior.attached)
                    snapshot.ended = prior.ended
                elif snapshot.attached.last_cursor == prior.attached.last_cursor:
                    if snapshot.attached != prior.attached:
                        raise HistoryConflictError("conflicting attachment snapshots at one cursor")
                    snapshot.ended = snapshot.ended or prior.ended
            row.feed_state = snapshot.SerializeToString()

    async def read(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> tuple[int, list[event_log_pb2.EventEntry]]:
        """Internal replay only; never expose this as an unscoped workload read API."""
        page = await self.read_page(session_id, after_cursor=after_cursor, limit=limit)
        return page.last_cursor, list(page.entries)

    async def read_page(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> protocol_pb2.ReadSessionEventsResponse:
        """Read prefix watermark and lifecycle from the same history-row snapshot."""
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid history page")
        async with self._sessions() as session:
            history = await session.get(SessionHistory, session_id)
            if history is None:
                raise HistoryNotFoundError(session_id)
            if after_cursor > history.last_cursor:
                raise ValueError("cursor beyond stored prefix")
            rows = await session.scalars(
                select(SessionEvent)
                .where(
                    SessionEvent.session_id == session_id,
                    SessionEvent.cursor > after_cursor,
                    SessionEvent.cursor <= history.last_cursor,
                )
                .order_by(SessionEvent.cursor)
                .limit(limit)
            )
            page = protocol_pb2.ReadSessionEventsResponse(
                last_cursor=history.last_cursor,
                entries=[event_log_pb2.EventEntry.FromString(row.payload) for row in rows],
            )
            if history.feed_state is not None:
                page.feed_state.ParseFromString(history.feed_state)
            return page

    async def read_observations(
        self, session_id: UUID, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> tuple[int, list[tuple[int, str]]]:
        """Seek a bounded metadata window in the retained prefix, including deleted Sandboxes."""
        if (
            not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid observation page")
        async with self._sessions() as session:
            history = await session.get(SessionHistory, session_id)
            if history is None:
                raise HistoryNotFoundError(session_id)
            query = select(SessionEvent.cursor, SessionEvent.payload).where(
                SessionEvent.session_id == session_id, SessionEvent.cursor <= history.last_cursor
            )
            if after_cursor is not None:
                query = query.where(SessionEvent.cursor > after_cursor).order_by(SessionEvent.cursor)
            else:
                if before_cursor is not None:
                    query = query.where(SessionEvent.cursor < before_cursor)
                query = query.order_by(SessionEvent.cursor.desc())
            # Stream rows so native payloads are not accumulated into a metadata page.
            rows = await session.stream(query.limit(limit).execution_options(yield_per=1))
            observations = [
                (cursor, event_log_pb2.EventEntry.FromString(payload).event.WhichOneof("observation") or "")
                async for cursor, payload in rows
            ]
            if after_cursor is None:
                observations.reverse()
            return history.last_cursor, observations
