"""Service-backed archive reads and app-owned Session projection metadata."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events.debug import ArchivedObservation, ArchivedObservationEntry, ObservationPage
from agentplane.app.threads.models import EventLog, ThreadCheckpoint, ThreadHistorySummary
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.client import SandboxServiceClient

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service:protocol_pb2


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
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        history_reader: SandboxServiceClient,
        history_creator: SandboxServiceClient | None = None,
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history_reader = history_reader
        self._history_creator = history_creator

    async def open(self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec) -> UUID:
        """Materialize the Session's Thread; preserve any existing legacy mapping."""
        # New service-owned IDs are canonical UUIDs. Old runner IDs (typically s-UUID)
        # retain their already-minted Thread IDs; discovery and explicit Open race safely
        # on the (sandbox, session_id) uniqueness constraint.
        try:
            parsed = UUID(session_id)
            thread_id = parsed if str(parsed) == session_id else uuid4()
        except ValueError:
            thread_id = uuid4()
        # Migrated Sessions retain their old runner locator but discovery now returns
        # their public UUID. Resolve either identity without rewriting that locator.
        existing_query = select(EventLog).where(EventLog.sandbox == sandbox)
        if str(thread_id) == session_id:
            existing_query = existing_query.where(or_(EventLog.session_id == session_id, EventLog.id == thread_id))
        else:
            existing_query = existing_query.where(EventLog.session_id == session_id)
        if self._history_creator is not None:
            async with self._sessions() as session:
                existing = await session.scalar(existing_query)
                if existing is not None:
                    # Discovery does not reconstruct missing projection metadata from raw rows.
                    # Existing app identities and summaries remain authoritative for the UI.
                    return existing.id
            if str(thread_id) != session_id:
                raise EventReplicationError("new service-projected Thread requires a canonical public Session ID")
            # Confirm durable service registration before admitting an app projection.
            # Do not hold an app DB transaction over this RPC or copy its event payload.
            await self._history_creator.read_session_observations(session_id, limit=1)
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(EventLog)
                .values(
                    id=thread_id,
                    sandbox=sandbox,
                    session_id=session_id,
                    harness=Harness(protocol_pb2.Harness.Name(spec.harness)),
                    model=spec.model,
                    cwd=spec.cwd,
                    # Preserve old-table write rejection until schema/trigger retirement.
                    raw_ingestion_fenced_at_cursor=0 if self._history_creator is not None else None,
                )
                .on_conflict_do_nothing()
                .returning(EventLog.id)
            )
            if created is not None:
                if self._history_creator is not None:
                    session.add(ThreadHistorySummary(thread_id=created))
                await notify(session, Channel.THREADS)
                return created
            existing = (await session.scalars(existing_query)).one_or_none()
            if existing is None:
                raise EventReplicationError("public Session ID belongs to another sandbox")
            # Another replica may win this insert race. Preserve its mapping and
            # projection state rather than reinitializing an existing Thread.
            return existing.id

    async def has_projection_metadata(self, thread_id: UUID) -> bool:
        async with self._sessions() as session:
            return await session.get(ThreadHistorySummary, thread_id) is not None

    async def resume_pending(self, thread_id: UUID) -> None:
        """A runner-confirmed restart supersedes a normal terminal feed, not a replay error.

        The ingester still owns the archived cursor and the next attached snapshot; this only
        prevents an SSE reader from mistaking the previous incarnation's end for the new one.
        """
        async with self._sessions.begin() as session:
            state = await session.get(ThreadHistorySummary, thread_id, with_for_update=True)
            if state is not None and state.end == {}:
                state.end = None
                if state.attached is not None:
                    state.resumed_after_cursor = ParseDict(state.attached, protocol_pb2.Attached()).last_cursor
                await notify(session, Channel.THREADS)

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
        """The committed app projection cursor, independent of archive progress."""
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread_id)
                )
            ) or 0

    async def projection_sessions(self) -> dict[UUID, RunnerSession]:
        """Sessions with app projection metadata, including deleted Sandboxes."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(EventLog.id, EventLog.sandbox, EventLog.session_id).join(
                    ThreadHistorySummary, ThreadHistorySummary.thread_id == EventLog.id
                )
            )
            return {row.id: RunnerSession(row.sandbox, row.session_id) for row in rows}

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
                raise ConnectionError("Sandbox Service history regressed behind the reader's cursor")
            if len(page.entries) > requested or any(
                entry.cursor != cursor + index + 1 or entry.cursor > page.last_cursor
                for index, entry in enumerate(page.entries)
            ):
                raise ConnectionError("Sandbox Service history page is not contiguous or exceeds its watermark")
            if not page.entries:
                if cursor < through:
                    raise ConnectionError("Sandbox Service omitted entries from a published prefix")
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
            raise ConnectionError("Requested observation is not committed in Sandbox Service history yet")
        if not page.entries:
            if cursor <= page.last_cursor:
                raise ConnectionError("Sandbox Service omitted a published entry")
            return None
        entry = page.entries[0]
        if len(page.entries) != 1 or entry.cursor != cursor or entry.cursor > page.last_cursor:
            raise ConnectionError("Sandbox Service returned an entry outside the requested committed position")
        return ArchivedObservationEntry(cursor=str(cursor), entry=MessageToDict(entry))

    async def feed_state(self, thread_id: UUID) -> FeedSnapshot | None:
        async with self._sessions() as session:
            state = await session.get(ThreadHistorySummary, thread_id)
            if state is None or state.attached is None:
                return None
            end = None if state.end is None else FeedError(state.end["message"]) if state.end else FeedEnd()
            return FeedSnapshot(ParseDict(state.attached, protocol_pb2.Attached()), end)


def project_attached(attached: protocol_pb2.Attached, entry: event_log_pb2.EventEntry) -> None:
    attached.last_cursor = entry.cursor
    event = entry.event
    match event.WhichOneof("observation"):
        case "setup_finished":
            attached.setup_state = (
                protocol_pb2.SETUP_STATE_SUCCEEDED
                if event.setup_finished.exit_code == 0
                else protocol_pb2.SETUP_STATE_FAILED
            )
        case "setup_interrupted":
            attached.setup_state = protocol_pb2.SETUP_STATE_INTERRUPTED
        case "harness_started":
            attached.harness_state = protocol_pb2.HARNESS_STATE_RUNNING
        case "harness_exited" | "harness_lost":
            attached.harness_state = protocol_pb2.HARNESS_STATE_STOPPED
            attached.active_turn_id = ""
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_changed":
            attached.spec.model = event.model_changed.model
        case "reasoning_effort_changed":
            attached.spec.reasoning_effort = event.reasoning_effort_changed.effort
