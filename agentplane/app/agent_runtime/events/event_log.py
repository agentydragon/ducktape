"""A runner session's Event log as the app copies it, and the feed state beside it.

Threads outlive sandboxes because of it: every runner event, `Native` frames included, is copied
into PostgreSQL as it arrives. A log is keyed by the sandbox and the client-chosen session id; its
entries are stored as the protocol's own proto-JSON under the source's follow cursor, so it reads
back without a runner and a deleted sandbox loses nothing.

`EventLogStore` opens a log and reads it. The functions below it write in the caller's session: they
are the event log's part of `Ingestion`'s writes, which commit with the fold's in one transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.app.agent_runtime.events.debug import ArchivedObservation, ArchivedObservationEntry, ObservationPage
from agentplane.app.presets import Harness
from agentplane.app.thread.models import Event, EventLog, FeedState
from agentplane.app.thread.updates import notify
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class EventReplicationError(ValueError):
    """The runner stream conflicts with the archived prefix or skips an entry."""

    def __init__(self, message: str, *, cursor: int | None = None) -> None:
        super().__init__(message)
        self.cursor = cursor


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


@dataclass(frozen=True)
class FeedEnd:
    pass


@dataclass(frozen=True)
class FeedError:
    message: str


@dataclass(frozen=True)
class FeedSnapshot:
    attached: protocol_pb2.Attached
    end: FeedEnd | FeedError | None


@dataclass(frozen=True)
class RunnerSession:
    """The runner session a log copies, which is where its thread's commands go."""

    sandbox: str
    session_id: str


class EventLogStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def open(self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec) -> UUID:
        """The session's event log, created from the spec on first sight; its id is the thread's."""
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(EventLog)
                .values(
                    sandbox=sandbox,
                    session_id=session_id,
                    harness=Harness(protocol_pb2.Harness.Name(spec.harness)),
                    model=spec.model,
                    cwd=spec.cwd,
                )
                .on_conflict_do_nothing(index_elements=[EventLog.sandbox, EventLog.session_id])
                .returning(EventLog.id)
            )
            if created is not None:
                await notify(session)
                return created
            return (
                await session.scalars(
                    select(EventLog.id).where(EventLog.sandbox == sandbox, EventLog.session_id == session_id)
                )
            ).one()

    async def find(self, sandbox: str, session_id: str) -> UUID | None:
        async with self._sessions() as session:
            return (
                await session.scalars(
                    select(EventLog.id).where(EventLog.sandbox == sandbox, EventLog.session_id == session_id)
                )
            ).one_or_none()

    async def runner_session(self, thread_id: UUID) -> RunnerSession | None:
        async with self._sessions() as session:
            log = (
                await session.execute(select(EventLog.sandbox, EventLog.session_id).where(EventLog.id == thread_id))
            ).one_or_none()
            return None if log is None else RunnerSession(log.sandbox, log.session_id)

    async def last_cursor(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
                )
                or 0
            )

    async def events(self, thread_id: UUID, *, after_cursor: int = 0, limit: int) -> list[event_log_pb2.EventEntry]:
        """Up to `limit` entries after the cursor, in cursor order; a reader pages until a short page."""
        async with self._sessions() as session:
            payloads = await session.scalars(
                select(Event.payload)
                .where(Event.thread_id == thread_id, Event.cursor > after_cursor)
                .order_by(Event.cursor)
                .limit(limit)
            )
            return [ParseDict(payload, event_log_pb2.EventEntry()) for payload in payloads]

    async def observations(
        self, thread_id: UUID, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> ObservationPage:
        """Seek directly into the immutable archive; never fold or load intervening history."""
        if (
            not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid chronological observation page bounds")
        async with self._sessions() as session:
            query = select(Event.cursor, Event.kind).where(Event.thread_id == thread_id)
            if after_cursor is not None:
                query = query.where(Event.cursor > after_cursor).order_by(Event.cursor)
            else:
                if before_cursor is not None:
                    query = query.where(Event.cursor < before_cursor)
                query = query.order_by(Event.cursor.desc())
            rows = list(await session.execute(query.limit(limit)))
            if after_cursor is None:
                rows.reverse()
            if not rows:
                return ObservationPage(observations=[], next_before_cursor=None, next_after_cursor=None)
            has_older = await session.scalar(
                select(select(Event.cursor).where(Event.thread_id == thread_id, Event.cursor < rows[0].cursor).exists())
            )
            has_newer = await session.scalar(
                select(
                    select(Event.cursor).where(Event.thread_id == thread_id, Event.cursor > rows[-1].cursor).exists()
                )
            )
            return ObservationPage(
                observations=[ArchivedObservation(cursor=str(row.cursor), kind=row.kind) for row in rows],
                next_before_cursor=str(rows[0].cursor) if has_older else None,
                next_after_cursor=str(rows[-1].cursor) if has_newer else None,
            )

    async def observation_entry(self, thread_id: UUID, cursor: int) -> ArchivedObservationEntry | None:
        """One raw archive entry, read only when a reader expands that observation."""
        async with self._sessions() as session:
            payload = await session.scalar(
                select(Event.payload).where(Event.thread_id == thread_id, Event.cursor == cursor)
            )
            return None if payload is None else ArchivedObservationEntry(cursor=str(cursor), entry=payload)

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        async with self._sessions() as session:
            state = await session.get(FeedState, thread_id)
            if state is None:
                return None
            end = None if state.end is None else FeedError(state.end["message"]) if state.end else FeedEnd()
            return FeedSnapshot(ParseDict(state.attached, protocol_pb2.Attached()), end)


def _project_attached(attached: protocol_pb2.Attached, entry: event_log_pb2.EventEntry) -> None:
    attached.last_cursor = entry.cursor
    event = entry.event
    match event.WhichOneof("observation"):
        case "harness_started":
            attached.harness_state = protocol_pb2.HARNESS_STATE_RUNNING
        case "harness_exited" | "harness_lost":
            attached.harness_state = protocol_pb2.HARNESS_STATE_STOPPED
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_changed":
            attached.spec.model = event.model_changed.model


async def append(
    session: AsyncSession, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry]
) -> list[event_log_pb2.EventEntry]:
    """Add the entries that extend the contiguous prefix and return them; a replayed entry must match."""
    last = await session.scalar(
        select(Event).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
    )
    cursor = last.cursor if last is not None else 0
    source_id = ParseDict(last.payload, event_log_pb2.EventEntry()).origin.source_id if last is not None else None
    payloads = dict(
        (
            await session.execute(
                select(Event.cursor, Event.payload).where(
                    Event.thread_id == thread_id,
                    Event.cursor.in_([entry.cursor for entry in entries if entry.cursor <= cursor]),
                )
            )
        )
        .tuples()
        .all()
    )
    inserted: list[event_log_pb2.EventEntry] = []
    for entry in entries:
        if not entry.cursor or not entry.origin.source_id or entry.origin.sequence != entry.cursor:
            raise EventReplicationError(f"invalid runner origin at cursor {entry.cursor}", cursor=entry.cursor)
        if source_id is not None and entry.origin.source_id != source_id:
            raise EventReplicationError(f"runner source changed at cursor {entry.cursor}", cursor=entry.cursor)
        payload = MessageToDict(entry)
        if entry.cursor in payloads:
            if payloads[entry.cursor] != payload:
                raise EventReplicationError(f"conflicting runner entry at cursor {entry.cursor}", cursor=entry.cursor)
            continue
        if entry.cursor != cursor + 1:
            raise EventReplicationError(
                f"expected runner cursor {cursor + 1}, received {entry.cursor}", cursor=entry.cursor
            )
        session.add(
            Event(
                thread_id=thread_id,
                cursor=entry.cursor,
                at=entry.event.at.ToDatetime(tzinfo=UTC),
                kind=entry.event.WhichOneof("observation") or "",
                payload=payload,
            )
        )
        payloads[entry.cursor] = payload
        inserted.append(entry)
        cursor = entry.cursor
        source_id = entry.origin.source_id
    return inserted


async def advance_feed(session: AsyncSession, thread_id: UUID, inserted: Sequence[event_log_pb2.EventEntry]) -> None:
    """Carry the feed's attachment snapshot, and the log's model with it, over newly added entries."""
    state = await session.get(FeedState, thread_id)
    if state is not None:
        attached = ParseDict(state.attached, protocol_pb2.Attached())
        previous_model = attached.spec.model
        for entry in inserted:
            # An Attached snapshot describes the runner at its cursor. Replaying the
            # earlier log fills history, but must not rewind that snapshot's state.
            if entry.cursor <= attached.last_cursor:
                continue
            _project_attached(attached, entry)
            if entry.event.HasField("harness_started"):
                state.end = None
        state.attached = MessageToDict(attached)
        if attached.spec.model != previous_model:
            await session.execute(update(EventLog).where(EventLog.id == thread_id).values(model=attached.spec.model))
        await session.flush()


async def set_attached(session: AsyncSession, thread_id: UUID, attached: protocol_pb2.Attached) -> None:
    state = await session.get(FeedState, thread_id)
    if state is not None and attached.last_cursor < ParseDict(state.attached, protocol_pb2.Attached()).last_cursor:
        raise ValueError("attachment snapshot is older than the committed feed state")
    values = {"attached": MessageToDict(attached), "end": None}
    await session.execute(
        insert(FeedState)
        .values(thread_id=thread_id, **values)
        .on_conflict_do_update(index_elements=[FeedState.thread_id], set_=values)
    )
    await session.execute(update(EventLog).where(EventLog.id == thread_id).values(model=attached.spec.model))


async def end_feed(session: AsyncSession, thread_id: UUID, error: str | None) -> None:
    state = await session.get(FeedState, thread_id)
    if state is None:
        raise ValueError("cannot end a feed before persisting its attachment")
    state.end = {} if error is None else {"message": error}
