"""Transactionally append and replay a runner's contiguous Event prefix."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.protocol import event_log_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.db import (
    SessionEvent,
    SessionEventSettledRange,
    SessionEventSettlement,
    SessionHistory,
)
from agentplane.sandbox_service.session_history.settlement import completion_target, settle, started_item

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


class HistoryConflictError(ValueError):
    """An entry skips or disagrees with the already stored runner prefix."""


class HistoryNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class OpenReservation:
    session_id: UUID
    launch_spec: bytes


@dataclass(frozen=True)
class HistoryLocator:
    session_id: UUID
    sandbox_name: str
    sandbox_uid: UUID
    runner_session_id: str


# An item whose start lies further back than this is kept whole rather than read in one transaction.
_SETTLEMENT_SPAN_LIMIT = 20_000


@dataclass(frozen=True)
class ObservationPage:
    last_cursor: int
    observations: list[tuple[int, str]]
    # Settled cursor ranges intersecting the page's window.
    settled: list[tuple[int, int]]


class Store:
    def __init__(self, engine: AsyncEngine, *, settle_deltas: bool) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        # The default for Sessions without their own `settle_deltas`.
        self._settle_deltas = settle_deltas

    async def runnable_locators(self, namespace: str, sandbox_names: list[str]) -> list[HistoryLocator]:
        """Only current-Sandbox locators; deleted/Suspended histories remain retained."""
        if not sandbox_names:
            return []
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SessionHistory).where(
                        SessionHistory.sandbox_namespace == namespace,
                        SessionHistory.sandbox_name.in_(sandbox_names),
                        SessionHistory.sandbox_uid.is_not(None),
                        SessionHistory.runner_session_id.is_not(None),
                    )
                )
            ).all()
            return [
                HistoryLocator(row.id, row.sandbox_name, row.sandbox_uid, row.runner_session_id)
                for row in rows
                if row.sandbox_uid is not None and row.runner_session_id is not None
            ]

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

    async def reserve(
        self,
        *,
        caller_namespace: str,
        caller_name: str,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        open_key: str,
        open_request: bytes,
        launch_spec: Callable[[UUID], bytes],
        settle_deltas: bool | None,
    ) -> OpenReservation:
        """Reserve a durable Session ID *before* runner attach, or recover a lost reply.

        The key is opaque to the Service and scoped to the authenticated caller and
        Sandbox incarnation. The request bytes identify the caller's original inputs;
        the launch factory freezes effective runner setup for the candidate ID.
        Committing before runner contact lets a retry recover the same setup.
        Imported histories use `open` to keep their existing public IDs.
        """
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name, open_key)):
            raise ValueError("caller, sandbox, and Open idempotency key are required")
        async with self._sessions.begin() as session:
            # Do not recompute today's defaults for an already reserved Open.
            query = select(SessionHistory).where(
                SessionHistory.caller_namespace == caller_namespace,
                SessionHistory.caller_name == caller_name,
                SessionHistory.sandbox_namespace == sandbox_namespace,
                SessionHistory.sandbox_name == sandbox_name,
                SessionHistory.sandbox_uid == sandbox_uid,
                SessionHistory.open_key == open_key,
            )
            existing = await session.scalar(query)
            if existing is not None:
                if existing.open_request != open_request:
                    raise HistoryConflictError("Open idempotency key reused with different inputs")
                assert existing.launch_spec is not None
                return OpenReservation(existing.id, existing.launch_spec)
            candidate = uuid4()
            frozen = launch_spec(candidate)
            created = await session.scalar(
                insert(SessionHistory)
                .values(
                    id=candidate,
                    caller_namespace=caller_namespace,
                    caller_name=caller_name,
                    sandbox_namespace=sandbox_namespace,
                    sandbox_name=sandbox_name,
                    sandbox_uid=sandbox_uid,
                    open_key=open_key,
                    open_request=open_request,
                    launch_spec=frozen,
                    runner_session_id=f"r-{candidate}",
                    source_id=None,
                    last_cursor=0,
                    settle_deltas=settle_deltas,
                )
                .on_conflict_do_nothing()
                .returning(SessionHistory.id)
            )
            if created is not None:
                return OpenReservation(created, frozen)
            existing = await session.scalar(query)
            if existing is None:
                raise HistoryConflictError("Open identity could not be reserved")
            if existing.open_request != open_request:
                raise HistoryConflictError("Open idempotency key reused with different inputs")
            assert existing.launch_spec is not None
            return OpenReservation(existing.id, existing.launch_spec)

    async def lookup_open(
        self,
        *,
        caller_namespace: str,
        caller_name: str,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        open_key: str,
    ) -> UUID | None:
        """Return only the public ID, scoped identically to a reservation; never return stored specs."""
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name, open_key)):
            raise ValueError("caller, sandbox, and Open idempotency key are required")
        async with self._sessions() as session:
            return cast(
                UUID | None,
                await session.scalar(
                    select(SessionHistory.id).where(
                        SessionHistory.caller_namespace == caller_namespace,
                        SessionHistory.caller_name == caller_name,
                        SessionHistory.sandbox_namespace == sandbox_namespace,
                        SessionHistory.sandbox_name == sandbox_name,
                        SessionHistory.sandbox_uid == sandbox_uid,
                        SessionHistory.open_key == open_key,
                    )
                ),
            )

    async def runner_id(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> str:
        """Resolve a public Session ID only in its original Sandbox incarnation."""
        async with self._sessions() as session:
            row = await session.get(SessionHistory, session_id)
            if (
                row is None
                or (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid)
                != (sandbox_namespace, sandbox_name, sandbox_uid)
                or row.runner_session_id is None
            ):
                raise HistoryNotFoundError(session_id)
            return row.runner_session_id

    async def session_id(
        self, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID, runner_session_id: str
    ) -> UUID | None:
        """Map an inventoried runner ID to a public Session ID, if managed."""
        async with self._sessions() as session:
            result = await session.scalar(
                select(SessionHistory.id).where(
                    SessionHistory.sandbox_namespace == sandbox_namespace,
                    SessionHistory.sandbox_name == sandbox_name,
                    SessionHistory.sandbox_uid == sandbox_uid,
                    SessionHistory.runner_session_id == runner_session_id,
                )
            )
            return cast(UUID | None, result)

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
            if self._settle_deltas if row.settle_deltas is None else row.settle_deltas:
                for entry in appended:
                    if completion_target(entry) is not None:
                        await _settle(session, session_id, entry)
            return row.last_cursor

    async def record_feed_state(self, session_id: UUID, feed: protocol_pb2.SessionFeedState) -> None:
        """Record only a covered runner snapshot; delayed copiers cannot rewind it."""
        snapshot = protocol_pb2.SessionFeedState()
        snapshot.CopyFrom(feed)
        async with self._sessions.begin() as session:
            row = await session.scalar(select(SessionHistory).where(SessionHistory.id == session_id).with_for_update())
            if row is None:
                raise HistoryNotFoundError(session_id)
            if not snapshot.HasField("attached") or snapshot.attached.session_id != row.runner_session_id:
                raise HistoryConflictError("feed snapshot does not match the bound runner session")
            if snapshot.attached.last_cursor > row.last_cursor:
                raise HistoryConflictError("feed snapshot is ahead of committed history")
            if row.feed_state is not None:
                prior = protocol_pb2.SessionFeedState.FromString(row.feed_state)
                if snapshot.attached.last_cursor < prior.attached.last_cursor:
                    return
                if snapshot.attached.last_cursor == prior.attached.last_cursor:
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
            page = protocol_pb2.ReadSessionEventsResponse(
                last_cursor=history.last_cursor,
                entries=[event_log_pb2.EventEntry.FromString(row.payload) for row in rows],
                settlements=[protocol_pb2.SettledDeltas.FromString(payload) for payload in settlements],
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


async def _settled_range(session: AsyncSession, session_id: UUID, cursor: int) -> SessionEventSettledRange | None:
    candidate = await session.scalar(
        select(SessionEventSettledRange)
        .where(SessionEventSettledRange.session_id == session_id, SessionEventSettledRange.first_cursor <= cursor)
        .order_by(SessionEventSettledRange.first_cursor.desc())
        .limit(1)
    )
    return candidate if candidate is not None and candidate.last_cursor >= cursor else None


async def _settle(session: AsyncSession, session_id: UUID, completion: event_log_pb2.EventEntry) -> None:
    """Settle the field `completion` completes, if its span qualifies; otherwise store nothing."""
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
            return
        for cursor, payload in rows:
            if floor is not None and cursor < floor:
                break
            entry = event_log_pb2.EventEntry.FromString(payload)
            span.append(entry)
            if floor is None and started_item(entry) == target.item_id:
                # The item's native start frame precedes its derived ItemStarted.
                floor = min(cursor, *entry.event.source_sequences)
        upper = rows[-1].cursor
    span.reverse()
    settled = settle(span)
    if settled is None:
        return
    session.add(
        SessionEventSettlement(
            session_id=session_id, completion_cursor=completion.cursor, payload=settled.SerializeToString()
        )
    )
    await session.flush()
    for cursor_range in settled.ranges:
        session.add(
            SessionEventSettledRange(
                session_id=session_id,
                first_cursor=cursor_range.first,
                last_cursor=cursor_range.last,
                completion_cursor=completion.cursor,
            )
        )
        await session.execute(
            delete(SessionEvent).where(
                SessionEvent.session_id == session_id,
                SessionEvent.cursor >= cursor_range.first,
                SessionEvent.cursor <= cursor_range.last,
            )
        )
