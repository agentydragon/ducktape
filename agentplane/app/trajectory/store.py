"""Trajectories outlive sandboxes: every runner event, `Native` frames included, copied into
PostgreSQL as it arrives.

A thread is one runner session, keyed by the sandbox and the client-chosen session id; its entries
are stored as the protocol's own proto-JSON under the source's follow cursor, so a thread reads back
without a runner and a deleted sandbox loses nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agentplane.app import thread_fold
from agentplane.app.changes import Changes
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import Harness
from agentplane.app.thread_debug import (
    ArchivedObservation,
    ArchivedObservationEntry,
    EvidenceObservation,
    EvidencePage,
    NativeFrame,
    NativeFramePage,
    ObservationPage,
    ThreadEvidenceNotFoundError,
    ThreadScopeChangedError,
)
from agentplane.app.trajectory.models import (
    Event,
    FeedState,
    SandboxIngestion,
    Thread,
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadNativeLink,
    ThreadPayloadManifest,
)
from agentplane.app.trajectory.recording import (
    EventReplicationError,
    ThreadFoldError,
    record_thread_fold,
    set_operational,
)
from agentplane.app.trajectory.updates import TrajectoryUpdates, notify
from agentplane.app.trajectory.views import SEGMENT_KINDS, EntityKind, ThreadCommandState, ThreadView
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


@dataclass(frozen=True)
class IngestionLease:
    sandbox: str
    token: UUID


class IngestionLeaseLostError(Exception):
    """The sandbox ingester no longer owns authority to commit observations."""


class ThreadInterestExpiredError(ValueError):
    """A bounded browser interest must be resolved again at the current projection position."""


class ThreadScopeResetError(ValueError):
    """A browser's retained projection source or epoch is no longer current."""


class CommandIdConflictError(ValueError):
    """A Thread command id was already admitted with a different immutable Command."""


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
class ThreadScope:
    projection_epoch: str
    through_cursor: int


@dataclass(frozen=True)
class ThreadEntityInterest:
    scope: ThreadScope
    anchor_cursor: int
    tail_from: int
    window_from: int | None = None
    window_before: int | None = None


@dataclass(frozen=True)
class ThreadPayloadSelection:
    scope: ThreadScope
    owner_cursor: int
    owner_id: str
    field: str
    generation: int
    revision_cursor: int
    chunk_count: int
    content_bytes: int


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


class TrajectoryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.operator_sessions = OperatorSessionStore(engine)
        self.changes = Changes()
        self._updates = TrajectoryUpdates(engine.url, self.changes)

    @classmethod
    def connect(cls, database_url: str) -> TrajectoryStore:
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
        """The thread for a session, created from its spec on first sight."""
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(Thread)
                .values(
                    sandbox=sandbox,
                    session_id=session_id,
                    harness=Harness(protocol_pb2.Harness.Name(spec.harness)),
                    model=spec.model,
                    cwd=spec.cwd,
                )
                .on_conflict_do_nothing(index_elements=[Thread.sandbox, Thread.session_id])
                .returning(Thread.id)
            )
            if created is not None:
                await notify(session)
                return created
            return (
                await session.scalars(
                    select(Thread.id).where(Thread.sandbox == sandbox, Thread.session_id == session_id)
                )
            ).one()

    async def last_cursor(self, thread_id: UUID) -> int:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
                )
                or 0
            )

    async def current_scope(self, thread_id: UUID) -> ThreadScope | None:
        """The sole epoch scope currently materialized for a Thread."""
        async with self._sessions() as session:
            checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
            if checkpoint is None:
                return None
            return ThreadScope(checkpoint.projection_epoch, checkpoint.through_cursor)

    async def entity_interest(
        self,
        thread_id: UUID,
        *,
        anchor_cursor: int | None = None,
        before_cursor: int | None = None,
        page_size: int = 30,
    ) -> ThreadEntityInterest | None:
        if not 1 <= page_size <= 100:
            raise ValueError("entity page size must be between 1 and 100")
        async with self._sessions() as session:
            checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
            if checkpoint is None:
                return None
            scope = ThreadScope(checkpoint.projection_epoch, checkpoint.through_cursor)
            anchor = scope.through_cursor if anchor_cursor is None else anchor_cursor
            if anchor < 0 or anchor > scope.through_cursor:
                raise ValueError("anchor is outside the projected prefix")
            common = (
                ThreadEntity.thread_id == thread_id,
                ThreadEntity.projection_epoch == scope.projection_epoch,
                ThreadEntity.entity_kind.in_(SEGMENT_KINDS),
            )
            if anchor_cursor is not None:
                newer = list(
                    await session.scalars(
                        select(ThreadEntity.cursor)
                        .where(*common, ThreadEntity.cursor > anchor)
                        .order_by(ThreadEntity.cursor)
                        .limit(page_size * 2 + 1)
                    )
                )
                if len(newer) > page_size * 2:
                    raise ThreadInterestExpiredError("entity interest must rotate")

            async def lower(before: int) -> int:
                cursors = list(
                    await session.scalars(
                        select(ThreadEntity.cursor)
                        .where(*common, ThreadEntity.cursor < before)
                        .order_by(ThreadEntity.cursor.desc())
                        .limit(page_size)
                    )
                )
                return cursors[-1] if cursors else before

            tail_from = await lower(anchor + 1)
            if before_cursor is None:
                return ThreadEntityInterest(scope, anchor, tail_from)
            if before_cursor < 0:
                raise ValueError("before cursor cannot be negative")
            return ThreadEntityInterest(scope, anchor, tail_from, await lower(before_cursor), before_cursor)

    async def payload_selection(
        self, thread_id: UUID, *, owner_cursor: int, owner_id: str, field: str, generation: int, revision_cursor: int
    ) -> ThreadPayloadSelection | None:
        async with self._sessions() as session:
            checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
            if checkpoint is None:
                return None
            manifest = await session.scalar(
                select(ThreadPayloadManifest).where(
                    ThreadPayloadManifest.thread_id == thread_id,
                    ThreadPayloadManifest.projection_epoch == checkpoint.projection_epoch,
                    ThreadPayloadManifest.owner_cursor == owner_cursor,
                    ThreadPayloadManifest.owner_id == owner_id,
                    ThreadPayloadManifest.field == field,
                    ThreadPayloadManifest.generation == generation,
                    ThreadPayloadManifest.revision_cursor == revision_cursor,
                )
            )
            if manifest is None:
                return None
            return ThreadPayloadSelection(
                ThreadScope(manifest.projection_epoch, checkpoint.through_cursor),
                owner_cursor,
                owner_id,
                field,
                generation,
                revision_cursor,
                manifest.chunk_count,
                manifest.content_bytes,
            )

    async def evidence(
        self, thread_id: UUID, *, projection_epoch: str, entity_kind: str, entity_id: str, after_cursor: int, limit: int
    ) -> EvidencePage:
        if not 1 <= limit <= 200 or after_cursor < 0:
            raise ValueError("invalid evidence page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(session, thread_id, projection_epoch, entity_kind, entity_id)
            native_exists = (
                select(ThreadNativeLink.source_sequence)
                .where(
                    ThreadNativeLink.thread_id == ThreadEvidence.thread_id,
                    ThreadNativeLink.projection_epoch == ThreadEvidence.projection_epoch,
                    ThreadNativeLink.entity_cursor == ThreadEvidence.entity_cursor,
                    ThreadNativeLink.observation_cursor == ThreadEvidence.observation_cursor,
                )
                .exists()
            )
            rows = list(
                await session.execute(
                    select(ThreadEvidence.observation_cursor, native_exists)
                    .where(
                        ThreadEvidence.thread_id == thread_id,
                        ThreadEvidence.projection_epoch == projection_epoch,
                        ThreadEvidence.entity_cursor == entity_cursor,
                        ThreadEvidence.observation_cursor > after_cursor,
                    )
                    .order_by(ThreadEvidence.observation_cursor)
                    .limit(limit + 1)
                )
            )
            return EvidencePage(
                observations=[
                    EvidenceObservation(observation_cursor=str(cursor), has_native=native)
                    for cursor, native in rows[:limit]
                ],
                next_after_cursor=str(rows[limit - 1][0]) if len(rows) > limit else None,
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
        if not 1 <= limit <= 200 or after_sequence < 0:
            raise ValueError("invalid native frame page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(session, thread_id, projection_epoch, entity_kind, entity_id)
            association = await session.get(
                ThreadEvidence, (thread_id, projection_epoch, entity_cursor, observation_cursor)
            )
            if association is None:
                raise ThreadEvidenceNotFoundError("no evidence association for the selected observation")
            rows = list(
                await session.execute(
                    select(ThreadNativeLink.source_sequence, Event.payload)
                    .outerjoin(
                        Event,
                        (
                            (Event.thread_id == ThreadNativeLink.thread_id)
                            & (Event.cursor == ThreadNativeLink.source_sequence)
                            & (Event.kind == "native")
                        ),
                    )
                    .where(
                        ThreadNativeLink.thread_id == thread_id,
                        ThreadNativeLink.projection_epoch == projection_epoch,
                        ThreadNativeLink.entity_cursor == entity_cursor,
                        ThreadNativeLink.observation_cursor == observation_cursor,
                        ThreadNativeLink.source_sequence > after_sequence,
                    )
                    .order_by(ThreadNativeLink.source_sequence)
                    .limit(limit + 1)
                )
            )
            return NativeFramePage(
                frames=[
                    NativeFrame.model_validate(
                        {
                            "source_sequence": str(sequence),
                            "availability": "present" if payload is not None else "unavailable",
                            "entry": payload,
                        }
                    )
                    for sequence, payload in rows[:limit]
                ],
                next_after_sequence=str(rows[limit - 1][0]) if len(rows) > limit else None,
            )

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

    async def command_outcomes(
        self, thread_id: UUID, projection_epoch: str, command_ids: Sequence[str]
    ) -> dict[str, thread_fold.CommandOutcome | None]:
        """Current outcomes for a finite browser-held command-id set, keyed by entity primary key."""
        requested = tuple(dict.fromkeys(command_ids))
        async with self._sessions() as session:
            if await session.get(Thread, thread_id) is None:
                raise ThreadNotFoundError(thread_id)
            checkpoint = await session.get(ThreadCheckpoint, thread_id)
            if checkpoint is None or checkpoint.projection_epoch != projection_epoch:
                raise ThreadScopeResetError("thread fold scope was reset")
            rows = await session.scalars(
                select(ThreadEntity).where(
                    ThreadEntity.thread_id == thread_id,
                    ThreadEntity.projection_epoch == projection_epoch,
                    ThreadEntity.entity_kind == EntityKind.COMMAND,
                    ThreadEntity.entity_id.in_(requested),
                )
            )
            outcomes = {row.entity_id: ThreadCommandState.model_validate(row.state).outcome for row in rows}
            return {command_id: outcomes.get(command_id) for command_id in requested}

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        """Atomically extend the contiguous prefix, accepting only identical replayed entries."""
        if not entries:
            return
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            last = await session.scalar(
                select(Event).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
            )
            cursor = last.cursor if last is not None else 0
            source_id = (
                ParseDict(last.payload, event_log_pb2.EventEntry()).origin.source_id if last is not None else None
            )
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
                        raise EventReplicationError(
                            f"conflicting runner entry at cursor {entry.cursor}", cursor=entry.cursor
                        )
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
                    await session.execute(
                        update(Thread).where(Thread.id == thread_id).values(model=attached.spec.model)
                    )
                await session.flush()
            await notify(session)

    async def acquire_ingestion(self, sandbox: str, duration: timedelta) -> IngestionLease | None:
        _positive_duration(duration)
        token = uuid4()
        async with self._sessions.begin() as session:
            acquired = await session.scalar(
                insert(SandboxIngestion)
                .values(sandbox=sandbox, token=token, expires_at=func.clock_timestamp() + duration)
                .on_conflict_do_update(
                    index_elements=[SandboxIngestion.sandbox],
                    set_={"token": token, "expires_at": func.clock_timestamp() + duration},
                    where=SandboxIngestion.expires_at <= func.clock_timestamp(),
                )
                .returning(SandboxIngestion.token)
            )
        return IngestionLease(sandbox, token) if acquired is not None else None

    async def renew_ingestion(self, lease: IngestionLease, duration: timedelta) -> bool:
        _positive_duration(duration)
        async with self._sessions.begin() as session:
            renewed = await session.scalar(
                update(SandboxIngestion)
                .where(
                    SandboxIngestion.sandbox == lease.sandbox,
                    SandboxIngestion.token == lease.token,
                    SandboxIngestion.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp() + duration)
                .returning(SandboxIngestion.token)
            )
        return renewed is not None

    async def release_ingestion(self, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(SandboxIngestion).where(
                    SandboxIngestion.sandbox == lease.sandbox, SandboxIngestion.token == lease.token
                )
            )

    async def set_attached(self, thread_id: UUID, attached: protocol_pb2.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if (
                state is not None
                and attached.last_cursor < ParseDict(state.attached, protocol_pb2.Attached()).last_cursor
            ):
                raise ValueError("attachment snapshot is older than the committed feed state")
            values = {"attached": MessageToDict(attached), "end": None}
            await session.execute(
                insert(FeedState)
                .values(thread_id=thread_id, **values)
                .on_conflict_do_update(index_elements=[FeedState.thread_id], set_=values)
            )
            await session.execute(update(Thread).where(Thread.id == thread_id).values(model=attached.spec.model))
            await set_operational(session, thread_id, status="active", error=None)
            await notify(session)

    async def end_feed(
        self, thread_id: UUID, *, lease: IngestionLease, error: str | None, error_cursor: int | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            await _fence(session, lease, thread_id)
            state = await session.get(FeedState, thread_id)
            if state is None:
                raise ValueError("cannot end a feed before persisting its attachment")
            state.end = {} if error is None else {"message": error}
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
            state = await session.get(FeedState, thread_id)
            if state is None:
                return None
            end = None if state.end is None else FeedError(state.end["message"]) if state.end else FeedEnd()
            return FeedSnapshot(ParseDict(state.attached, protocol_pb2.Attached()), end)

    async def list_threads(
        self, *, sandbox: str | None = None, session_id: str | None = None, include_archived: bool = False
    ) -> list[ThreadView]:
        """Newest first; each filter given narrows the list to threads matching it. Archived
        threads are excluded unless asked for, mirroring the Sandbox inventory's own default."""
        last_cursor = (
            select(Event.cursor)
            .where(Event.thread_id == Thread.id)
            .order_by(Event.cursor.desc())
            .limit(1)
            .scalar_subquery()
        )
        last_at = (
            select(Event.at).where(Event.thread_id == Thread.id).order_by(Event.at.desc()).limit(1).scalar_subquery()
        )
        query = (
            select(Thread, last_cursor, last_at, FeedState.attached)
            .outerjoin(FeedState, FeedState.thread_id == Thread.id)
            .order_by(Thread.created_at.desc())
        )
        if sandbox is not None:
            query = query.where(Thread.sandbox == sandbox)
        if session_id is not None:
            query = query.where(Thread.session_id == session_id)
        if not include_archived:
            query = query.where(Thread.archived.is_(False))
        async with self._sessions() as session:
            return [
                _view(thread, last_cursor, last_at, attached)
                for thread, last_cursor, last_at, attached in await session.execute(query)
            ]

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                return None
            return _view(thread, *await _last(session, thread_id))

    async def admitted_command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        """The archived admission of this immutable command, if the Thread has one.

        This is deliberately an archive lookup rather than a command outbox. A matching result
        lets a retry recover a lost HTTP response without contacting a possibly deleted Sandbox;
        a reused id with different work is a conflict, never an implicit new command.
        """
        async with self._sessions() as session:
            if await session.get(Thread, thread_id) is None:
                raise ThreadNotFoundError(thread_id)
            checkpoint = await session.get(ThreadCheckpoint, thread_id)
            if checkpoint is None:
                return None
            summary = await session.get(
                ThreadEntity, (thread_id, checkpoint.projection_epoch, EntityKind.COMMAND, command.command_id)
            )
            if summary is None:
                return None
            payload = await session.scalar(
                select(Event.payload).where(Event.thread_id == thread_id, Event.cursor == summary.cursor)
            )
            if payload is None:
                raise ValueError("command summary has no archived admission")
            entry = ParseDict(payload, event_log_pb2.EventEntry())
            if (
                not entry.event.HasField("command_admitted")
                or entry.event.command_admitted.command.command_id != command.command_id
            ):
                raise ValueError("command summary does not point to its archived admission")
            admitted = entry.event.command_admitted.command
            if admitted == command:
                return entry
            raise CommandIdConflictError(f"command id {command.command_id!r} was already admitted with different work")

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            thread.name = name
            await session.flush()
            renamed = _view(thread, *await _last(session, thread_id))
            await notify(session)
        return renamed

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        async with self._sessions.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            thread.archived = archived
            await session.flush()
            view = _view(thread, *await _last(session, thread_id))
            await notify(session)
        return view

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


async def _evidence_entity_cursor(
    session: AsyncSession, thread_id: UUID, projection_epoch: str, entity_kind: str, entity_id: str
) -> int:
    checkpoint = await session.get(ThreadCheckpoint, thread_id)
    if checkpoint is None:
        raise ThreadEvidenceNotFoundError("no materialized thread fold")
    if checkpoint.projection_epoch != projection_epoch:
        raise ThreadScopeChangedError("the thread fold projection epoch has changed")
    entity = await session.get(ThreadEntity, (thread_id, projection_epoch, entity_kind, entity_id))
    if entity is None:
        raise ThreadEvidenceNotFoundError("no selected thread entity")
    return entity.cursor


def _positive_duration(duration: timedelta) -> None:
    if duration <= timedelta(0):
        raise ValueError("ingestion lease duration must be positive")


async def _fence(session: AsyncSession, lease: IngestionLease, thread_id: UUID) -> None:
    # Lock before reading database time: a transaction that waited on an owner must not rely on
    # its transaction-start timestamp. Takeover/renewal waits until this write commits or rolls back.
    owned = await session.scalar(
        select(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox).with_for_update()
    )
    now = (await session.scalars(select(func.clock_timestamp()))).one()
    if owned is None or owned.token != lease.token or owned.expires_at <= now:
        raise IngestionLeaseLostError(lease.sandbox)
    sandbox = await session.scalar(select(Thread.sandbox).where(Thread.id == thread_id))
    if sandbox != lease.sandbox:
        raise IngestionLeaseLostError("lease does not own this thread's sandbox")


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


async def _last(session: AsyncSession, thread_id: UUID) -> tuple[int | None, datetime | None, dict[str, object] | None]:
    last_cursor = await session.scalar(
        select(Event.cursor).where(Event.thread_id == thread_id).order_by(Event.cursor.desc()).limit(1)
    )
    last_at = await session.scalar(
        select(Event.at).where(Event.thread_id == thread_id).order_by(Event.at.desc()).limit(1)
    )
    state = await session.get(FeedState, thread_id)
    return last_cursor, last_at, (state.attached if state is not None else None)


def _view(
    thread: Thread, last_cursor: int | None, last_at: datetime | None, attached: dict[str, object] | None
) -> ThreadView:
    harness_state = (
        ParseDict(attached, protocol_pb2.Attached()).harness_state
        if attached is not None
        else protocol_pb2.HARNESS_STATE_UNSPECIFIED
    )
    return ThreadView(
        id=thread.id,
        sandbox=thread.sandbox,
        session_id=thread.session_id,
        harness=thread.harness,
        model=thread.model,
        cwd=thread.cwd,
        created_at=thread.created_at,
        name=thread.name,
        archived=thread.archived,
        last_cursor=last_cursor or 0,
        last_event_at=last_at,
        harness_state=protocol_pb2.HarnessState.Name(harness_state),
    )
