"""`ThreadStore`, the thread component's API over PostgreSQL.

It owns the engine and each transaction: it runs the levels' functions inside one, composes the
writes that span levels, and sends the change notice after a write. What is set on a thread
itself, its name and archive state, is read and written here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from google.protobuf.json_format import ParseDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agentplane.app import thread_fold
from agentplane.app.changes import Changes
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.thread import content, event_log, ingestion_lease
from agentplane.app.thread.content import ThreadEntityInterest, ThreadPayloadSelection, ThreadScope
from agentplane.app.thread.event_log import EventReplicationError, FeedSnapshot, ThreadNotFoundError
from agentplane.app.thread.ingestion_lease import IngestionLease
from agentplane.app.thread.models import Event, EventLog, FeedState, Thread
from agentplane.app.thread.recording import ThreadFoldError, record_thread_fold, set_operational
from agentplane.app.thread.updates import ThreadUpdates, notify
from agentplane.app.thread.views import ThreadView
from agentplane.app.thread_debug import ArchivedObservationEntry, EvidencePage, NativeFramePage, ObservationPage
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


class ThreadStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.operator_sessions = OperatorSessionStore(engine)
        self.changes = Changes()
        self._updates = ThreadUpdates(engine.url, self.changes)

    @classmethod
    def connect(cls, database_url: str) -> ThreadStore:
        return cls(create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True))

    async def close(self) -> None:
        await self._updates.close()
        await self._engine.dispose()

    async def start_updates(self) -> None:
        await self._updates.start()

    @property
    def updates_connected(self) -> bool:
        return self._updates.connected

    async def thread(self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec) -> UUID:
        """The thread for a session: its event log, created from the spec on first sight."""
        async with self._sessions.begin() as session:
            created = await event_log.create(session, sandbox, session_id, spec)
            if created is not None:
                await notify(session)
                return created
            return await event_log.id_of(session, sandbox, session_id)

    async def last_cursor(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return await event_log.last_cursor(session, thread_id)

    async def current_scope(self, thread_id: UUID) -> ThreadScope | None:
        async with self._sessions() as session:
            return await content.current_scope(session, thread_id)

    async def entity_interest(
        self,
        thread_id: UUID,
        *,
        anchor_cursor: int | None = None,
        before_cursor: int | None = None,
        page_size: int = 30,
    ) -> ThreadEntityInterest | None:
        async with self._sessions() as session:
            return await content.entity_interest(
                session, thread_id, anchor_cursor=anchor_cursor, before_cursor=before_cursor, page_size=page_size
            )

    async def payload_selection(
        self, thread_id: UUID, *, owner_cursor: int, owner_id: str, field: str, generation: int, revision_cursor: int
    ) -> ThreadPayloadSelection | None:
        async with self._sessions() as session:
            return await content.payload_selection(
                session,
                thread_id,
                owner_cursor=owner_cursor,
                owner_id=owner_id,
                field=field,
                generation=generation,
                revision_cursor=revision_cursor,
            )

    async def evidence(
        self, thread_id: UUID, *, projection_epoch: str, entity_kind: str, entity_id: str, after_cursor: int, limit: int
    ) -> EvidencePage:
        async with self._sessions() as session:
            return await content.evidence(
                session,
                thread_id,
                projection_epoch=projection_epoch,
                entity_kind=entity_kind,
                entity_id=entity_id,
                after_cursor=after_cursor,
                limit=limit,
            )

    async def native_frames(
        self,
        thread_id: UUID,
        *,
        projection_epoch: str,
        entity_kind: str,
        entity_id: str,
        observation_cursor: int,
        after_sequence: int,
        limit: int,
    ) -> NativeFramePage:
        async with self._sessions() as session:
            return await content.native_frames(
                session,
                thread_id,
                projection_epoch=projection_epoch,
                entity_kind=entity_kind,
                entity_id=entity_id,
                observation_cursor=observation_cursor,
                after_sequence=after_sequence,
                limit=limit,
            )

    async def observations(
        self, thread_id: UUID, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> ObservationPage:
        async with self._sessions() as session:
            return await event_log.observations(
                session, thread_id, before_cursor=before_cursor, after_cursor=after_cursor, limit=limit
            )

    async def observation_entry(self, thread_id: UUID, cursor: int) -> ArchivedObservationEntry | None:
        async with self._sessions() as session:
            return await event_log.observation_entry(session, thread_id, cursor)

    async def command_outcomes(
        self, thread_id: UUID, projection_epoch: str, command_ids: Sequence[str]
    ) -> dict[str, thread_fold.CommandOutcome | None]:
        async with self._sessions() as session:
            return await content.command_outcomes(session, thread_id, projection_epoch, command_ids)

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        """Atomically extend the contiguous prefix, accepting only identical replayed entries."""
        if not entries:
            return
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            inserted = await event_log.append(session, thread_id, entries)
            if not inserted:
                return
            try:
                await record_thread_fold(session, thread_id, inserted[0].origin.source_id, inserted)
            except EventReplicationError:
                raise
            except (thread_fold.ObservationNotUnderstoodError, thread_fold.FoldContractError) as error:
                raise ThreadFoldError(
                    f"thread fold failed at cursor {error.cursor}: {error}", cursor=error.cursor
                ) from error
            except ValueError as error:
                raise ThreadFoldError(f"thread fold failed: {error}") from error
            # The maximum stored cursor is the checkpoint: the fenced transaction admits
            # only a contiguous suffix, so there is no separately mutable progress counter.
            await event_log.advance_feed(session, thread_id, inserted)
            await notify(session)

    async def acquire_ingestion(self, sandbox: str, duration: timedelta) -> IngestionLease | None:
        async with self._sessions.begin() as session:
            return await ingestion_lease.acquire(session, sandbox, duration)

    async def renew_ingestion(self, lease: IngestionLease, duration: timedelta) -> bool:
        async with self._sessions.begin() as session:
            return await ingestion_lease.renew(session, lease, duration)

    async def release_ingestion(self, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.release(session, lease)

    async def set_attached(self, thread_id: UUID, attached: protocol_pb2.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            await event_log.set_attached(session, thread_id, attached)
            await set_operational(session, thread_id, status="active", error=None)
            await notify(session)

    async def end_feed(
        self, thread_id: UUID, *, lease: IngestionLease, error: str | None, error_cursor: int | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            await event_log.end_feed(session, thread_id, error)
            await set_operational(
                session,
                thread_id,
                status="ended" if error is None else "failed",
                error=error,
                error_cursor=error_cursor,
            )
            await session.flush()
            await notify(session)

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        async with self._sessions() as session:
            return await event_log.feed_state(session, thread_id)

    async def list_threads(
        self, *, sandbox: str | None = None, session_id: str | None = None, include_archived: bool = False
    ) -> list[ThreadView]:
        """Newest first; each filter given narrows the list to threads matching it. Archived
        threads are excluded unless asked for, mirroring the Sandbox inventory's own default."""
        last_cursor = (
            select(Event.cursor)
            .where(Event.thread_id == EventLog.id)
            .order_by(Event.cursor.desc())
            .limit(1)
            .scalar_subquery()
        )
        last_at = (
            select(Event.at).where(Event.thread_id == EventLog.id).order_by(Event.at.desc()).limit(1).scalar_subquery()
        )
        query = (
            select(EventLog, Thread, last_cursor, last_at, FeedState.attached)
            .outerjoin(Thread, Thread.id == EventLog.id)
            .outerjoin(FeedState, FeedState.thread_id == EventLog.id)
            .order_by(EventLog.created_at.desc())
        )
        if sandbox is not None:
            query = query.where(EventLog.sandbox == sandbox)
        if session_id is not None:
            query = query.where(EventLog.session_id == session_id)
        if not include_archived:
            query = query.where(Thread.archived.is_not(True))
        async with self._sessions() as session:
            return [
                _view(log, thread, last_cursor, last_at, attached)
                for log, thread, last_cursor, last_at, attached in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            log = await session.get(EventLog, thread_id)
            if log is None:
                return None
            return _view(log, await session.get(Thread, thread_id), *await _last(session, thread_id))

    async def admitted_command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        async with self._sessions() as session:
            return await content.admitted_command(session, thread_id, command)

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            renamed = await _set_thread(session, thread_id, name=name)
            await notify(session)
        return renamed

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        async with self._sessions.begin() as session:
            view = await _set_thread(session, thread_id, archived=archived)
            await notify(session)
        return view

    async def events(self, thread_id: UUID, *, after_cursor: int = 0, limit: int) -> list[event_log_pb2.EventEntry]:
        async with self._sessions() as session:
            return await event_log.events(session, thread_id, after_cursor=after_cursor, limit=limit)


async def _last(session: AsyncSession, thread_id: UUID) -> tuple[int | None, datetime | None, dict[str, object] | None]:
    last_cursor = await session.scalar(
        select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
    )
    last_at = await session.scalar(
        select(Event.at).where(Event.thread_id == thread_id).order_by(Event.at.desc()).limit(1)
    )
    state = await session.get(FeedState, thread_id)
    return last_cursor, last_at, (state.attached if state is not None else None)


async def _set_thread(session: AsyncSession, thread_id: UUID, **values: object) -> ThreadView:
    """Write what an operator set on a thread, creating its row the first time."""
    log = await session.get(EventLog, thread_id)
    if log is None:
        raise ThreadNotFoundError(thread_id)
    thread = await session.scalar(
        insert(Thread)
        .values(id=thread_id, **values)
        .on_conflict_do_update(index_elements=[Thread.id], set_=values)
        .returning(Thread)
    )
    return _view(log, thread, *await _last(session, thread_id))


def _view(
    log: EventLog,
    thread: Thread | None,
    last_cursor: int | None,
    last_at: datetime | None,
    attached: dict[str, object] | None,
) -> ThreadView:
    harness_state = (
        ParseDict(attached, protocol_pb2.Attached()).harness_state
        if attached is not None
        else protocol_pb2.HARNESS_STATE_UNSPECIFIED
    )
    return ThreadView(
        id=log.id,
        sandbox=log.sandbox,
        session_id=log.session_id,
        harness=log.harness,
        model=log.model,
        cwd=log.cwd,
        created_at=log.created_at,
        name=None if thread is None else thread.name,
        archived=thread is not None and thread.archived,
        last_cursor=last_cursor or 0,
        last_event_at=last_at,
        harness_state=protocol_pb2.HarnessState.Name(harness_state),
    )
