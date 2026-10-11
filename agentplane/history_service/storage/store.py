"""Transactionally append and replay a runner's contiguous Event prefix."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.storage.db import (
    SessionEvent,
    SessionEventSettledRange,
    SessionEventSettlement,
    SessionHistory,
)
from agentplane.history_service.storage.history_changes import notify_history
from agentplane.history_service.storage.settlement import completion_target, settle, started_item
from agentplane.proto import event_log_pb2, runner_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


class HistoryConflictError(ValueError):
    """An entry skips or disagrees with the already stored runner prefix."""


class HistoryNotFoundError(LookupError):
    pass


class WriterMismatchError(PermissionError):
    """The Session is bound to another writer."""


@dataclass(frozen=True)
class Writer:
    """The ServiceAccount a Session's ingress pushes come from; the UID tells apart an account
    recreated under the same name."""

    namespace: str
    name: str
    uid: str


@dataclass(frozen=True)
class Checkpoint:
    cursor: int
    # The seal at `cursor` is committed; nothing more is accepted.
    sealed: bool


# An item whose start lies further back than this is kept whole rather than read in one transaction.
_SETTLEMENT_SPAN_LIMIT = 20_000


@dataclass(frozen=True)
class ObservationPage:
    last_cursor: int
    observations: list[tuple[int, str]]
    # Settled cursor ranges intersecting the page's window.
    settled: list[tuple[int, int]]


class Store:
    def __init__(self, engine: AsyncEngine, *, settle_deltas: bool = False) -> None:
        """`settle_deltas`: `append` removes an item's streamed delta entries once its completion provably
        holds their concatenation. Only the runner ingress sets it."""
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._settle_deltas = settle_deltas

    async def bind_writer(self, session_id: UUID, writer: Writer) -> Checkpoint:
        """Bind the Session to `writer` if it has no writer yet, creating its row; refuse any other.

        Of two concurrent first writers, the row lock lets one bind and refuses the other.
        """
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(SessionHistory)
                .values(
                    id=session_id,
                    writer_namespace=writer.namespace,
                    writer_name=writer.name,
                    writer_uid=writer.uid,
                    source_id=None,
                    last_cursor=0,
                )
                .on_conflict_do_nothing()
                .returning(SessionHistory.id)
            )
            row = await session.get(SessionHistory, session_id, with_for_update=True, populate_existing=True)
            assert row is not None
            if row.writer_uid is None:
                row.writer_namespace, row.writer_name, row.writer_uid = writer.namespace, writer.name, writer.uid
            elif (row.writer_namespace, row.writer_name, row.writer_uid) != (writer.namespace, writer.name, writer.uid):
                raise WriterMismatchError(session_id)
            if created is not None:
                await notify_history(session)
            return _checkpoint(row)

    async def append(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> int:
        """Replay exact duplicates or extend the prefix; serialize concurrent writers by Session ID.

        Validation, settlement of the items the batch completes, and the new checkpoint commit
        together. On any error the entire batch rolls back. An empty batch is a checkpoint read,
        not an inferred runner high-water mark.
        """
        async with self._sessions.begin() as session:
            row = await session.scalar(select(SessionHistory).where(SessionHistory.id == session_id).with_for_update())
            if row is None:
                raise HistoryNotFoundError(session_id)
            appended: list[event_log_pb2.EventEntry] = []
            for entry in entries:
                cursor = entry.cursor
                if cursor == 0 or not entry.origin.source_id or entry.origin.sequence != cursor:
                    raise HistoryConflictError(f"invalid runner origin at {cursor}")
                if row.source_id is not None and row.source_id != entry.origin.source_id:
                    raise HistoryConflictError(f"source changed at {cursor}")
                payload = entry.SerializeToString(deterministic=True)
                if cursor <= row.last_cursor:
                    existing = await session.get(SessionEvent, (session_id, cursor))
                    if existing is None and await _settled_range(session, session_id, cursor) is not None:
                        continue  # a settled delta; its exact payload is no longer stored to compare
                    if existing is None or existing.payload != payload:
                        raise HistoryConflictError(f"conflicting entry at {cursor}")
                    continue
                if cursor != row.last_cursor + 1:
                    raise HistoryConflictError(f"expected {row.last_cursor + 1}, received {cursor}")
                session.add(SessionEvent(session_id=session_id, cursor=cursor, payload=payload))
                row.source_id = entry.origin.source_id
                row.last_cursor = cursor
                appended.append(entry)
            if self._settle_deltas:
                for entry in appended:
                    if completion_target(entry) is not None and (
                        settled := await settlement_for(session, session_id, entry)
                    ):
                        await record_settlement(session, session_id, entry.cursor, settled)
            if appended:
                await notify_history(session)
            return row.last_cursor

    async def record_feed_state(self, session_id: UUID, feed: history_pb2.SessionFeedState) -> None:
        """Record only a covered runner snapshot; delayed copiers cannot rewind it or drop a seal.

        The snapshot may name the runner session or the public Session ID; it is stored under the
        runner's where the row knows it (rows the retired follower created).
        """
        snapshot = history_pb2.SessionFeedState()
        snapshot.CopyFrom(feed)
        async with self._sessions.begin() as session:
            row = await session.scalar(select(SessionHistory).where(SessionHistory.id == session_id).with_for_update())
            if row is None:
                raise HistoryNotFoundError(session_id)
            if not snapshot.HasField("attached") or snapshot.attached.session_id not in (
                row.runner_session_id,
                str(row.id),
            ):
                raise HistoryConflictError("feed snapshot does not match the bound runner session")
            # Rows the ingress created know only the public ID.
            snapshot.attached.session_id = row.runner_session_id or str(row.id)
            if snapshot.attached.last_cursor > row.last_cursor:
                raise HistoryConflictError("feed snapshot is ahead of committed history")
            if snapshot.HasField("sealed") and snapshot.sealed.cursor != row.last_cursor:
                raise HistoryConflictError("seal does not end the committed history")
            if row.feed_state is not None:
                prior = history_pb2.SessionFeedState.FromString(row.feed_state)
                if prior.HasField("sealed"):
                    if snapshot.HasField("sealed") and snapshot.sealed != prior.sealed:
                        raise HistoryConflictError("conflicting seals")
                    snapshot.sealed.CopyFrom(prior.sealed)
                if snapshot.attached.last_cursor < prior.attached.last_cursor:
                    snapshot.attached.CopyFrom(prior.attached)
                    snapshot.ended = prior.ended
                elif snapshot.attached.last_cursor == prior.attached.last_cursor:
                    if snapshot.attached != prior.attached:
                        same_except_harness = runner_pb2.Attached()
                        same_except_harness.CopyFrom(prior.attached)
                        same_except_harness.harness_state = snapshot.attached.harness_state
                        if same_except_harness != snapshot.attached:
                            raise HistoryConflictError("conflicting attachment snapshots at one cursor")
                        if (
                            not prior.HasField("sealed")
                            and prior.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
                            and snapshot.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
                            and not snapshot.ended
                        ):
                            # Resume can be confirmed before HarnessStarted advances the journal.
                            pass
                        elif (
                            prior.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
                            and snapshot.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
                        ):
                            # Stops have journal entries. A stopped snapshot at the same cursor
                            # after Resume belongs to the previous run.
                            snapshot.CopyFrom(prior)
                        else:
                            raise HistoryConflictError("conflicting attachment snapshots at one cursor")
                    else:
                        snapshot.ended = snapshot.ended or prior.ended
            serialized = snapshot.SerializeToString(deterministic=True)
            if row.feed_state != serialized:
                row.feed_state = serialized
                await notify_history(session)

    async def read(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> tuple[int, list[event_log_pb2.EventEntry]]:
        """Internal replay only; never expose this as an unscoped workload read API."""
        page = await self.read_page(session_id, after_cursor=after_cursor, limit=limit)
        return page.last_cursor, list(page.entries)

    async def read_page(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> history_pb2.ReadSessionEventsResponse:
        """Read prefix watermark and lifecycle from the same history-row snapshot."""
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid history page")
        async with self._sessions() as session:
            # One snapshot: settlement deletes entries and adds their range in one commit, so a page
            # read across that commit could pair entries with the range that removed them.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            history = await session.get(SessionHistory, session_id)
            if history is None:
                raise HistoryNotFoundError(session_id)
            if after_cursor > history.last_cursor:
                raise ValueError("cursor beyond stored prefix")
            rows = (
                await session.scalars(
                    select(SessionEvent)
                    .where(
                        SessionEvent.session_id == session_id,
                        SessionEvent.cursor > after_cursor,
                        SessionEvent.cursor <= history.last_cursor,
                    )
                    .order_by(SessionEvent.cursor)
                    .limit(limit)
                )
            ).all()
            through = rows[-1].cursor if rows else after_cursor
            covering = await session.scalars(
                select(SessionEventSettledRange.completion_cursor).where(
                    SessionEventSettledRange.session_id == session_id,
                    SessionEventSettledRange.first_cursor <= through,
                    SessionEventSettledRange.last_cursor > after_cursor,
                )
            )
            completing = await session.scalars(
                select(SessionEventSettlement.completion_cursor).where(
                    SessionEventSettlement.session_id == session_id,
                    SessionEventSettlement.completion_cursor > after_cursor,
                    SessionEventSettlement.completion_cursor <= through,
                )
            )
            settlements = await session.scalars(
                select(SessionEventSettlement.payload)
                .where(
                    SessionEventSettlement.session_id == session_id,
                    SessionEventSettlement.completion_cursor.in_({*covering, *completing}),
                )
                .order_by(SessionEventSettlement.completion_cursor)
            )
            page = history_pb2.ReadSessionEventsResponse(
                last_cursor=history.last_cursor,
                entries=[event_log_pb2.EventEntry.FromString(row.payload) for row in rows],
                settlements=[history_pb2.SettledDeltas.FromString(payload) for payload in settlements],
            )
            if history.feed_state is not None:
                page.feed_state.ParseFromString(history.feed_state)
            return page

    async def read_observations(
        self, session_id: UUID, *, before_cursor: int | None = None, after_cursor: int | None = None, limit: int = 30
    ) -> ObservationPage:
        """Seek a bounded metadata window in the retained prefix, including deleted Sandboxes."""
        if (
            not 1 <= limit <= 200
            or (before_cursor is not None and before_cursor < 0)
            or (after_cursor is not None and after_cursor < 0)
            or (before_cursor is not None and after_cursor is not None)
        ):
            raise ValueError("invalid observation page")
        async with self._sessions() as session:
            # One snapshot, as in `read_page`.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
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
            # The window runs from the requested bound to the far end of what was returned.
            if after_cursor is not None:
                low, high = after_cursor + 1, observations[-1][0] if observations else after_cursor
            else:
                high = history.last_cursor if before_cursor is None else min(history.last_cursor, before_cursor - 1)
                low = observations[0][0] if observations else high + 1
            ranges = await session.execute(
                select(SessionEventSettledRange.first_cursor, SessionEventSettledRange.last_cursor)
                .where(
                    SessionEventSettledRange.session_id == session_id,
                    SessionEventSettledRange.first_cursor <= high,
                    SessionEventSettledRange.last_cursor >= low,
                )
                .order_by(SessionEventSettledRange.first_cursor)
            )
            return ObservationPage(history.last_cursor, observations, [(first, last) for first, last in ranges])


def _checkpoint(row: SessionHistory) -> Checkpoint:
    sealed = row.feed_state is not None and history_pb2.SessionFeedState.FromString(row.feed_state).HasField("sealed")
    return Checkpoint(cursor=row.last_cursor, sealed=sealed)


async def _settled_range(session: AsyncSession, session_id: UUID, cursor: int) -> SessionEventSettledRange | None:
    candidate = await session.scalar(
        select(SessionEventSettledRange)
        .where(SessionEventSettledRange.session_id == session_id, SessionEventSettledRange.first_cursor <= cursor)
        .order_by(SessionEventSettledRange.first_cursor.desc())
        .limit(1)
    )
    return candidate if candidate is not None and candidate.last_cursor >= cursor else None


async def settlement_for(
    session: AsyncSession, session_id: UUID, completion: event_log_pb2.EventEntry
) -> history_pb2.SettledDeltas | None:
    """The settlement of the field `completion` completes, if its stored span qualifies."""
    target = completion_target(completion)
    assert target is not None
    span = [completion]
    floor: int | None = None
    upper = completion.cursor
    while floor is None or upper > floor:
        rows = (
            await session.execute(
                select(SessionEvent.cursor, SessionEvent.payload)
                .where(SessionEvent.session_id == session_id, SessionEvent.cursor < upper)
                .order_by(SessionEvent.cursor.desc())
                .limit(256)
            )
        ).all()
        if not rows or len(span) + len(rows) > _SETTLEMENT_SPAN_LIMIT:
            return None
        for cursor, payload in rows:
            if floor is not None and cursor < floor:
                break
            entry = event_log_pb2.EventEntry.FromString(payload)
            span.append(entry)
            if floor is None and started_item(entry) == target.item_id:
                # The item's native start frame precedes its derived ItemStarted.
                floor = min([cursor, *entry.event.source_sequences])
        upper = rows[-1].cursor
    span.reverse()
    return settle(span)


async def record_settlement(
    session: AsyncSession, session_id: UUID, completion_cursor: int, settled: history_pb2.SettledDeltas
) -> None:
    """Store `settled` and delete the entries in its ranges."""
    session.add(
        SessionEventSettlement(
            session_id=session_id, completion_cursor=completion_cursor, payload=settled.SerializeToString()
        )
    )
    await session.flush()
    for cursor_range in settled.ranges:
        session.add(
            SessionEventSettledRange(
                session_id=session_id,
                first_cursor=cursor_range.first,
                last_cursor=cursor_range.last,
                completion_cursor=completion_cursor,
            )
        )
        await session.execute(
            delete(SessionEvent).where(
                SessionEvent.session_id == session_id,
                SessionEvent.cursor >= cursor_range.first,
                SessionEvent.cursor <= cursor_range.last,
            )
        )
