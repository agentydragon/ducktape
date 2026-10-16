"""App Thread identities for fixture Sessions, opened in the scripted history database as well."""

from typing import override
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.database_updates import Channel
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.models import Thread
from agentplane.app.threads.summaries import Summaries
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History
from agentplane.postgres.wakeups import notify

SANDBOX_NAMESPACE = "test-history-sandbox-namespace"


def fixture_session_id(sandbox: str, session_id: str) -> UUID:
    """The public ID a fixture's Session alias stands for; a canonical UUID stands for itself."""
    try:
        return UUID(session_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, sandbox + "/" + session_id)


class SeededEventLogStore(EventLogStore):
    """Opens a fixture's Session in the history database as well; every read is production."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        history: History | None,
        history_reader: HistoryServiceClient,
        summaries: Summaries,
    ) -> None:
        self.engine = engine
        self.history = history
        self.reader = history_reader
        super().__init__(engine, history_reader=history_reader, summaries=summaries)

    @override
    async def open(self, session_id: str) -> UUID:
        """Open the Thread of a Session the app discovered, without the History Service's confirmation:
        a fixture's runner may reach the History Service only through the app's own ingestion."""
        public_id = UUID(session_id)
        await self._insert(public_id)
        return public_id

    async def register(self, sandbox: str, session_id: str) -> UUID:
        """Open a fixture Session in History; a non-UUID `session_id` is a stable test alias."""
        public_id = fixture_session_id(sandbox, session_id)
        if self.history is not None:
            await self.history.open(public_id, sandbox_name=sandbox)
        return public_id

    async def seed(self, sandbox: str, session_id: str) -> UUID:
        """Register a fixture's Session and open its Thread."""
        public_id = await self.register(sandbox, session_id)
        await self._insert(public_id)
        return public_id

    async def _insert(self, public_id: UUID) -> None:
        async with self._sessions.begin() as session:
            if await session.scalar(insert(Thread).values(id=public_id).on_conflict_do_nothing().returning(Thread.id)):
                await notify(session, Channel.THREADS)
