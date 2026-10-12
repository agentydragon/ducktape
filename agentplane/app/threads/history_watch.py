"""Follow the History Service's watch: wake app workers when committed state changes, and open a Thread
for each Session it records, whoever started the Session."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from sqlalchemy.exc import SQLAlchemyError

from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.history_service.client import HistoryServiceClient

logger = logging.getLogger(__name__)


class _Rewatch(ABC):
    """Follows one watch stream after another until cancelled."""

    def __init__(self, history: HistoryServiceClient, *, retry_interval_s: float, fresh_channel_after: int) -> None:
        """`history` is this watch's own client: a fresh channel must not close shared read RPCs."""
        if retry_interval_s <= 0 or fresh_channel_after < 1:
            raise ValueError("retry interval and fresh-channel threshold must be positive")
        self._history = history
        self._retry_interval_s = retry_interval_s
        self._fresh_channel_after = fresh_channel_after

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                async for _ in self._follow():
                    failures = 0
                failures = 0
            except ConnectionError, TimeoutError, SQLAlchemyError:
                failures += 1
                logger.warning("History Service watch failed; reconnecting", exc_info=True)
                if failures % self._fresh_channel_after == 0:
                    logger.warning("history watch failed %d times in a row; dialing a fresh channel", failures)
                    await self._history.redial()
                await asyncio.sleep(self._retry_interval_s)

    @abstractmethod
    def _follow(self) -> AsyncIterator[None]:
        """Follow one stream to planned renewal, yielding at each step of progress."""


class HistoryWatch(_Rewatch):
    def __init__(
        self,
        history: HistoryServiceClient,
        changed: Callable[[], None],
        *,
        retry_interval_s: float,
        fresh_channel_after: int = 4,
    ) -> None:
        super().__init__(history, retry_interval_s=retry_interval_s, fresh_channel_after=fresh_channel_after)
        self._changed = changed

    async def _follow(self) -> AsyncIterator[None]:
        # Each stream opens with a notice, covering changes committed while it was down.
        async for _ in self._history.watch_session_summaries():
            self._changed()
            yield


class SessionOpenings(_Rewatch):
    """Opens a Thread for every Session the History Service records. Each stream resumes after the last
    Session this replica opened, so a reconnect misses none; a new process starts from the first, and
    `EventLogStore.open` leaves an existing Thread as it is."""

    def __init__(
        self,
        history: HistoryServiceClient,
        event_logs: EventLogStore,
        *,
        retry_interval_s: float,
        fresh_channel_after: int = 4,
    ) -> None:
        super().__init__(history, retry_interval_s=retry_interval_s, fresh_channel_after=fresh_channel_after)
        self._event_logs = event_logs
        self._opened_through = 0

    async def _follow(self) -> AsyncIterator[None]:
        async for opened in self._history.watch_session_openings(after_position=self._opened_through):
            # A failure leaves the position behind this Session, so the next stream offers it again.
            await self._event_logs.open(str(opened.session_id))
            self._opened_through = opened.position
            yield
