"""`ThreadStore`, the thread component's API over PostgreSQL.

A thread is assembled from its event log: listing and reading it join the log with what an operator
has set on the thread, its name and archive state, which is written here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from google.protobuf.json_format import ParseDict
from pydantic import JsonValue
from sqlalchemy import Select, case, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events.event_log import ThreadNotFoundError
from agentplane.app.threads.models import Event, EventLog, FeedState, Thread, ThreadCheckpoint, ThreadHistorySummary
from agentplane.app.threads.view.views import ThreadView
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class ThreadStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

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
        last_turn = _last_turn_completed(EventLog.id).scalar_subquery()
        fenced = EventLog.raw_ingestion_fenced_at_cursor.is_not(None)
        query = (
            select(
                EventLog,
                Thread,
                case((fenced, ThreadCheckpoint.through_cursor), else_=last_cursor),
                case((fenced, ThreadHistorySummary.last_event_at), else_=last_at),
                case((fenced, ThreadHistorySummary.attached), else_=FeedState.attached),
                case((fenced, ThreadHistorySummary.end), else_=FeedState.end),
                case((fenced, None), else_=last_turn),
                ThreadHistorySummary,
            )
            .outerjoin(Thread, Thread.id == EventLog.id)
            .outerjoin(ThreadCheckpoint, ThreadCheckpoint.thread_id == EventLog.id)
            .outerjoin(ThreadHistorySummary, ThreadHistorySummary.thread_id == EventLog.id)
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
                _view(log, thread, cursor, at, attached, end, turn, summary)
                for log, thread, cursor, at, attached, end, turn, summary in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            log = await session.get(EventLog, thread_id)
            if log is None:
                return None
            return _view(log, await session.get(Thread, thread_id), *await _last(session, thread_id))

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            renamed = await _set_thread(session, thread_id, name=name)
            await notify(session, Channel.THREADS)
        return renamed

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        async with self._sessions.begin() as session:
            view = await _set_thread(session, thread_id, archived=archived)
            await notify(session, Channel.THREADS)
        return view


def _last_turn_completed(thread_id: UUID | InstrumentedAttribute[UUID]) -> Select[tuple[dict[str, JsonValue]]]:
    """The thread's newest `turn_completed` entry. The kind is written inline rather than bound, so the
    planner can match the partial index whatever the plan cache holds."""
    return (
        select(Event.payload)
        .where(Event.thread_id == thread_id, Event.kind == literal_column("'turn_completed'"))
        .order_by(Event.cursor.desc())
        .limit(1)
    )


async def _last(
    session: AsyncSession, thread_id: UUID
) -> tuple[
    int | None,
    datetime | None,
    dict[str, object] | None,
    dict[str, str] | None,
    dict[str, JsonValue] | None,
    ThreadHistorySummary | None,
]:
    log = await session.get(EventLog, thread_id)
    state = await session.get(FeedState, thread_id)
    if log is not None and log.raw_ingestion_fenced_at_cursor is not None:
        summary = await session.get(ThreadHistorySummary, thread_id)
        if summary is None:
            raise ValueError("missing service projection metadata at raw fence")
        cursor = await session.scalar(
            select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread_id)
        )
        return (cursor, summary.last_event_at, summary.attached, summary.end, None, summary)
    last_cursor = await session.scalar(
        select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
    )
    last_at = await session.scalar(
        select(Event.at).where(Event.thread_id == thread_id).order_by(Event.at.desc()).limit(1)
    )
    return (
        last_cursor,
        last_at,
        (state.attached if state is not None else None),
        (state.end if state is not None else None),
        await session.scalar(_last_turn_completed(thread_id)),
        None,
    )


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
    end: dict[str, str] | None,
    last_turn: dict[str, JsonValue] | None,
    summary: ThreadHistorySummary | None,
) -> ThreadView:
    if log.raw_ingestion_fenced_at_cursor is not None:
        if summary is None:
            raise ValueError("missing service projection metadata at raw fence")
        last_turn_status = (
            None if summary.last_turn_status is None else event_pb2.TurnStatus.Name(summary.last_turn_status)
        )
    else:
        last_turn_status = None if last_turn is None else _turn_status(last_turn)
    attachment = ParseDict(attached, protocol_pb2.Attached()) if attached is not None else None
    harness_state = attachment.harness_state if attachment is not None else protocol_pb2.HARNESS_STATE_UNSPECIFIED
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
        last_model_activity_at=log.last_model_activity_at,
        harness_state=protocol_pb2.HarnessState.Name(harness_state),
        setup_state=protocol_pb2.SetupState.Name(attachment.setup_state) if attachment is not None else None,
        active_turn_id=(attachment.active_turn_id or None) if attachment is not None else None,
        feed_status=(None if attachment is None else "active" if end is None else "failed" if end else "ended"),
        reasoning_effort=attachment.spec.reasoning_effort if attachment is not None else None,
        last_turn_status=last_turn_status,
    )


def _turn_status(payload: dict[str, JsonValue]) -> str:
    """Parsed as the message it is: proto-JSON omits an UNSPECIFIED status, so it has no key to read."""
    return event_pb2.TurnStatus.Name(ParseDict(payload, event_log_pb2.EventEntry()).event.turn_completed.status)
