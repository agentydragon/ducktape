"""Service-backed archive reads and app-owned Session projection metadata."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from google.protobuf.json_format import MessageToDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events.debug import ArchivedObservation, ArchivedObservationEntry, ObservationPage
from agentplane.app.threads.models import EventLog
from agentplane.app.threads.summaries import AppSummaries, FeedSnapshot, HistorySummaries
from agentplane.history_service.client import HistoryServiceClient
from agentplane.protocol import event_log_pb2
from agentplane.thread_fold.models import ThreadHistorySummary
from agentplane.thread_fold.recording import EventReplicationError

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/history_service:protocol_pb2


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


@dataclass(frozen=True)
class ServiceSessionReference:
    """A public Sandbox Service Session identity and its Sandbox route."""

    sandbox: str
    session_id: str


class EventLogStore:
    def __init__(
        self, engine: AsyncEngine, *, history_reader: HistoryServiceClient, summaries: AppSummaries | HistorySummaries
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history_reader = history_reader
        self._summaries = summaries

    async def open(self, sandbox: str, session_id: str) -> UUID:
        """Materialize a public service Session without consulting private runner locators."""
        try:
            thread_id = UUID(session_id)
        except ValueError as error:
            raise EventReplicationError(
                "new service-projected Thread requires a canonical public Session ID"
            ) from error
        if str(thread_id) != session_id:
            raise EventReplicationError("new service-projected Thread requires a canonical public Session ID")
        existing_query = select(EventLog).where(EventLog.sandbox == sandbox, EventLog.id == thread_id)
        async with self._sessions() as session:
            existing = await session.scalar(existing_query)
            if existing is not None:
                # Discovery does not reconstruct missing projection metadata from raw rows.
                # Existing app identities and summaries remain authoritative for the UI.
                return existing.id
        # Confirm durable service registration before admitting an app projection.
        # Do not hold an app DB transaction over this RPC or copy its event payload.
        await self._history_reader.read_session_observations(session_id, limit=1)
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(EventLog).values(id=thread_id, sandbox=sandbox).on_conflict_do_nothing().returning(EventLog.id)
            )
            if created is not None:
                session.add(ThreadHistorySummary(thread_id=created))
                await notify(session, Channel.THREADS)
                return created
            existing = (await session.scalars(existing_query)).one_or_none()
            if existing is None:
                raise EventReplicationError("public Session ID belongs to another sandbox")
            # Another replica may win this insert race. Preserve its mapping and
            # projection state rather than reinitializing an existing Thread.
            return existing.id

    async def resume_pending(self, thread_id: UUID) -> None:
        """A runner-confirmed restart supersedes a normal terminal feed, not a replay error."""
        await self._summaries.resume_pending(thread_id)

    async def service_session(self, thread_id: UUID) -> ServiceSessionReference | None:
        async with self._sessions() as session:
            log = (
                await session.execute(select(EventLog.sandbox, EventLog.id).where(EventLog.id == thread_id))
            ).one_or_none()
            return None if log is None else ServiceSessionReference(log.sandbox, str(log.id))

    async def last_cursor(self, thread_id: UUID) -> int:
        """The committed fold cursor, independent of archive progress."""
        return await self._summaries.folded_through(thread_id)

    async def projection_sessions(self) -> dict[UUID, ServiceSessionReference]:
        """Sessions with app projection metadata, including deleted Sandboxes."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(EventLog.id, EventLog.sandbox).join(
                    ThreadHistorySummary, ThreadHistorySummary.thread_id == EventLog.id
                )
            )
            return {row.id: ServiceSessionReference(row.sandbox, str(row.id)) for row in rows}

    async def read_watermark(self, thread_id: UUID) -> int:
        """Return the selected archive's committed cursor, not the UI projection's cursor."""
        # Do not use the independently advancing app cursor as a service resume
        # position. Metadata avoids downloading an arbitrary native payload.
        page = await self._history_reader.read_session_observations(str(thread_id), limit=1)
        return page.last_cursor

    async def events(self, thread_id: UUID, *, after_cursor: int = 0, limit: int) -> list[event_log_pb2.EventEntry]:
        """Up to `limit` entries after the cursor, in cursor order; a reader pages until a short page."""
        result: list[event_log_pb2.EventEntry] = []
        cursor = after_cursor
        through: int | None = None
        while len(result) < limit:
            requested = min(limit - len(result), 1000)
            if through is not None:
                requested = min(requested, through - cursor)
                if requested == 0:
                    break
            page = await self._history_reader.read_session_events(str(thread_id), after_cursor=cursor, limit=requested)
            if through is None:
                through = page.last_cursor
            if page.last_cursor < through or cursor > page.last_cursor:
                raise ConnectionError("History Service regressed behind the reader's cursor")
            if len(page.entries) > requested or any(
                entry.cursor != cursor + index + 1 or entry.cursor > page.last_cursor
                for index, entry in enumerate(page.entries)
            ):
                raise ConnectionError("History Service page is not contiguous or exceeds its watermark")
            if not page.entries:
                if cursor < through:
                    raise ConnectionError("History Service omitted entries from a published prefix")
                break
            result.extend(entry for entry in page.entries if entry.cursor <= through)
            cursor = result[-1].cursor
        return result

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
        page = await self._history_reader.read_session_observations(
            str(thread_id), before_cursor=before_cursor, after_cursor=after_cursor, limit=limit
        )
        observations = page.observations
        # Service history is a contiguous, immutable prefix. Boundaries need no
        # app raw lookups and refer to the watermark captured by this response.
        if after_cursor is not None:
            start = after_cursor + 1
            end = min(page.last_cursor, after_cursor + limit)
        else:
            end = page.last_cursor if before_cursor is None else min(page.last_cursor, max(0, before_cursor - 1))
            start = max(1, end - limit + 1)
        if [row.cursor for row in observations] != list(range(start, end + 1)):
            raise ConnectionError("invalid service observation page")
        return ObservationPage(
            observations=[ArchivedObservation(cursor=str(row.cursor), kind=row.kind) for row in observations],
            next_before_cursor=str(observations[0].cursor) if observations and observations[0].cursor > 1 else None,
            next_after_cursor=str(observations[-1].cursor)
            if observations and observations[-1].cursor < page.last_cursor
            else None,
        )

    async def observation_entry(self, thread_id: UUID, cursor: int) -> ArchivedObservationEntry | None:
        """One raw archive entry, read only when a reader expands that observation."""
        if cursor < 1:
            return None
        page = await self._history_reader.read_session_events(str(thread_id), after_cursor=cursor - 1, limit=1)
        if page.last_cursor < cursor <= await self.last_cursor(thread_id):
            raise ConnectionError("Requested observation is not committed in History Service yet")
        if not page.entries:
            if cursor <= page.last_cursor:
                raise ConnectionError("History Service omitted a published entry")
            return None
        entry = page.entries[0]
        if len(page.entries) != 1 or entry.cursor != cursor or entry.cursor > page.last_cursor:
            raise ConnectionError("History Service returned an entry outside the requested committed position")
        return ArchivedObservationEntry(cursor=str(cursor), entry=MessageToDict(entry))

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        summary = await self._summaries.get(thread_id)
        return None if summary is None else summary.feed
