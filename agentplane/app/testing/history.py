"""App Thread identities for fixture Sessions, opened in the scripted history database as well."""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.models import EventLog
from agentplane.app.threads.summaries import Summaries
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History


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
        # None where a Sandbox Service shares the history database and registers its Sessions itself.
        self.engine = engine
        self.history = history
        self.reader = history_reader
        super().__init__(engine, history_reader=history_reader, summaries=summaries)

    async def open(self, sandbox: str, session_id: str) -> UUID:
        public_id = fixture_session_id(sandbox, session_id)
        async with self._sessions() as session:
            if (
                await session.scalar(select(EventLog.id).where(EventLog.sandbox == sandbox, EventLog.id == public_id))
                is not None
            ):
                return public_id
        if self.history is not None:
            await self.history.open(public_id, sandbox=sandbox)
        async with self._sessions.begin() as session:
            await session.execute(insert(EventLog).values(id=public_id, sandbox=sandbox).on_conflict_do_nothing())
            await notify(session, Channel.THREADS)
        return public_id
