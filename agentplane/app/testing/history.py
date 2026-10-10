"""Seed app identities and project evidence supplied by a controlled gRPC peer."""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.database_updates import Channel, notify
from agentplane.app.testing.history_service import HistoryService
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.events.projection_lease import ProjectionLease
from agentplane.app.threads.history_projector import HistoryProjector
from agentplane.app.threads.ingestion import Ingester, Ingestion
from agentplane.app.threads.models import EventLog, ThreadHistorySummary
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.view.recording import set_operational
from agentplane.history_service.client import HistoryServiceClient
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


class SeededEventLogStore(EventLogStore):
    """Seed imported identities for tests; all history and lifecycle reads are production."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        peer: HistoryService,
        history_reader: HistoryServiceClient,
        history_creator: HistoryServiceClient | None = None,
    ) -> None:
        self.engine = engine
        self.peer = peer
        self.reader = history_reader
        super().__init__(engine, history_reader=self.reader, history_creator=history_creator)

    async def find(self, sandbox: str, session_id: str) -> UUID | None:
        # Fixture aliases are deterministic public IDs, not production locator lookups.
        try:
            public_id = UUID(session_id)
        except ValueError:
            public_id = uuid5(NAMESPACE_URL, sandbox + "/" + session_id)
        return await super().find(sandbox, str(public_id))

    async def open(self, sandbox: str, session_id: str, spec: runner_pb2.SessionSpec) -> UUID:
        if self._history_creator is not None:
            return await super().open(sandbox, session_id, spec)
        existing = await self.find(sandbox, session_id)
        if existing is not None:
            return existing
        try:
            public_id = UUID(session_id)
        except ValueError:
            public_id = uuid5(NAMESPACE_URL, sandbox + "/" + session_id)
        self.peer.open(public_id)
        async with self._sessions.begin() as session:
            await session.execute(
                insert(EventLog)
                .values(
                    id=public_id,
                    sandbox=sandbox,
                    harness=Harness(runner_pb2.Harness.Name(spec.harness)),
                    model=spec.model,
                    cwd=spec.cwd,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(insert(ThreadHistorySummary).values(thread_id=public_id).on_conflict_do_nothing())
            await notify(session, Channel.THREADS)
        return public_id


class ProjectedHistory(Ingestion):
    """Fixture commands that publish external evidence then run the real app projector."""

    def __init__(self, engine: AsyncEngine, *, peer: HistoryService, history_reader: HistoryServiceClient) -> None:
        super().__init__(engine)
        self.peer = peer
        self.reader = history_reader
        self.projector = HistoryProjector(engine, history_reader)
        self._attachments: dict[UUID, runner_pb2.Attached] = {}

    async def project(self, thread_id: UUID, lease: ProjectionLease) -> None:
        while True:
            progress = await self.projector.project_batch(thread_id, lease=lease)
            if progress.through_cursor >= progress.service_cursor:
                return

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: ProjectionLease
    ) -> None:
        self.peer.publish(thread_id, entries)
        history = self.peer.histories[str(thread_id)]
        cursor = history.entries[-1].cursor if history.entries else 0
        pending = self._attachments.get(thread_id)
        if pending is not None and pending.last_cursor <= cursor:
            self.peer.set_feed(thread_id, protocol_pb2.SessionFeedState(attached=pending))
            del self._attachments[thread_id]
        await self.project(thread_id, lease)

    async def set_attached(self, thread_id: UUID, attached: runner_pb2.Attached, *, lease: ProjectionLease) -> None:
        snapshot = runner_pb2.Attached()
        snapshot.CopyFrom(attached)
        snapshot.session_id = str(thread_id)
        history = self.peer.histories[str(thread_id)]
        covered = (history.entries[-1].cursor if history.entries else 0) >= snapshot.last_cursor
        if not covered:
            # A fixture may describe a runner snapshot before seeding its entries.
            # Publish lifecycle metadata only when the scripted prefix covers it.
            self._attachments[thread_id] = snapshot
            return
        self.peer.set_feed(thread_id, protocol_pb2.SessionFeedState(attached=snapshot))
        await self.project(thread_id, lease)

    async def end_feed(
        self, thread_id: UUID, *, lease: ProjectionLease, error: str | None, error_cursor: int | None = None
    ) -> None:
        if error is not None:
            # Explicit projected failure state for UI tests, not a fake archive failure.
            async with self._sessions.begin() as session:
                await set_operational(session, thread_id, status="failed", error=error, error_cursor=error_cursor)
                await notify(session, Channel.THREADS)
            return
        feed = self.peer.histories[str(thread_id)].feed
        if feed is None:
            raise ValueError("seed an attachment before terminal state")
        terminal = protocol_pb2.SessionFeedState.FromString(feed.SerializeToString())
        terminal.ended = True
        self.peer.set_feed(thread_id, terminal)
        await self.project(thread_id, lease)


class ProjectedIngester(Ingester):
    """Configure the production app coordinator with its history RPC client."""

    def __init__(
        self,
        *,
        runners: SandboxSessions,
        event_logs: SeededEventLogStore,
        ingestion: ProjectedHistory,
        history_projector: HistoryProjector | None = None,
    ) -> None:
        super().__init__(
            runners=runners,
            event_logs=event_logs,
            ingestion=ingestion,
            history_projector=history_projector if history_projector is not None else ingestion.projector,
        )
