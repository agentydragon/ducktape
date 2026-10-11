"""Service-backed archive reads and the app's Thread → Session link."""

from __future__ import annotations

from itertools import pairwise
from uuid import UUID

from google.protobuf.json_format import MessageToDict
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel
from agentplane.app.threads.events.debug import ArchivedObservation, ArchivedObservationEntry, ObservationPage
from agentplane.app.threads.models import EventLog
from agentplane.app.threads.summaries import FeedSnapshot, Summaries
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.settled_cursors import next_stored, settled_ranges
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_log_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/proto:history_service_pb2


class SessionAdmissionError(ValueError):
    """A discovered Session cannot become a Thread."""


class ThreadNotFoundError(Exception):
    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"no thread {thread_id}")


class SessionNotRegisteredError(LookupError):
    """An app Thread whose Session the Runner Gateway has no record of."""

    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"the Runner Gateway has no Session {thread_id}")


class EventLogStore:
    def __init__(self, engine: AsyncEngine, *, history_reader: HistoryServiceClient, summaries: Summaries) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history_reader = history_reader
        self._summaries = summaries

    async def open(self, session_id: str) -> UUID:
        """Materialize a public service Session without consulting private runner locators."""
        try:
            thread_id = UUID(session_id)
        except ValueError as error:
            raise SessionAdmissionError("a new Thread requires a canonical public Session ID") from error
        if str(thread_id) != session_id:
            raise SessionAdmissionError("a new Thread requires a canonical public Session ID")
        if await self.exists(thread_id):
            return thread_id
        # Confirm durable service registration before admitting a Thread.
        # Do not hold an app DB transaction over this RPC or copy its event payload.
        await self._history_reader.read_session_observations(session_id, limit=1)
        async with self._sessions.begin() as session:
            # Another replica may win this insert race; its row is the same mapping.
            if await session.scalar(
                insert(EventLog).values(id=thread_id).on_conflict_do_nothing().returning(EventLog.id)
            ):
                await notify(session, Channel.THREADS)
        return thread_id

    async def resume_pending(self, thread_id: UUID) -> None:
        """A runner-confirmed restart supersedes a normal terminal feed, not a replay error."""
        await self._summaries.resume_pending(thread_id)

    async def exists(self, thread_id: UUID) -> bool:
        async with self._sessions() as session:
            return await session.get(EventLog, thread_id) is not None

    async def last_cursor(self, thread_id: UUID) -> int:
        """The committed fold cursor, independent of archive progress."""
        return await self._summaries.folded_through(thread_id)

    async def read_watermark(self, thread_id: UUID) -> int:
        """Return the archive's committed cursor, not the fold's."""
        # The fold advances independently and is no service resume position.
        # Metadata avoids downloading an arbitrary native payload.
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
            settled = settled_ranges(span for settlement in page.settlements for span in settlement.ranges)
            cursors = [entry.cursor for entry in page.entries]
            if (
                len(cursors) > requested
                or any(
                    following != next_stored(previous, settled) for previous, following in pairwise([cursor, *cursors])
                )
                or (cursors and cursors[-1] > page.last_cursor)
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
        # Service history is an immutable prefix whose only gaps are the settled ranges the page
        # reports. Boundaries need no app raw lookups and refer to the watermark captured by this
        # response. Cursor 1 and the head are never settled: a settled range lies strictly between
        # an item's start and its completion.
        cursors = [row.cursor for row in observations]
        settled = settled_ranges(page.settled)
        if after_cursor is not None:
            walk = [after_cursor, *cursors]
            within = not cursors or cursors[-1] <= page.last_cursor
            short_ok = (cursors[-1] if cursors else after_cursor) >= page.last_cursor
        else:
            end = page.last_cursor if before_cursor is None else min(page.last_cursor, max(0, before_cursor - 1))
            walk = cursors
            within = not cursors or cursors[-1] <= end < next_stored(cursors[-1], settled)
            short_ok = cursors[0] == 1 if cursors else end < 1
        if not (
            within
            and all(following == next_stored(previous, settled) for previous, following in pairwise(walk))
            and (len(cursors) == limit or short_ok)
        ):
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
        # The History Service refuses a read past its committed prefix, which only grows.
        if cursor > await self.read_watermark(thread_id):
            if cursor <= await self.last_cursor(thread_id):
                raise ConnectionError("Requested observation is not committed in History Service yet")
            return None
        page = await self._history_reader.read_session_events(str(thread_id), after_cursor=cursor - 1, limit=1)
        settled = settled_ranges(span for settlement in page.settlements for span in settlement.ranges)
        if next_stored(cursor - 1, settled) != cursor:
            return None  # a settled delta, no longer stored
        if len(page.entries) != 1 or page.entries[0].cursor != cursor:
            raise ConnectionError("History Service returned an entry outside the requested committed position")
        entry = page.entries[0]
        return ArchivedObservationEntry(cursor=str(cursor), entry=MessageToDict(entry))

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        return (await self._summaries.get(thread_id)).feed
