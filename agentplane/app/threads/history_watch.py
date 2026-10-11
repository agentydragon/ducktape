"""Wake app workers when committed History Service state changes."""

import asyncio
import logging
from collections.abc import Callable

from agentplane.history_service.client import HistoryServiceClient

logger = logging.getLogger(__name__)


class HistoryWatch:
    def __init__(
        self,
        history: HistoryServiceClient,
        changed: Callable[[], None],
        *,
        retry_interval_s: float,
        fresh_channel_after: int = 4,
    ) -> None:
        """`history` is this watch's own client: a fresh channel must not close shared read RPCs."""
        if retry_interval_s <= 0 or fresh_channel_after < 1:
            raise ValueError("retry interval and fresh-channel threshold must be positive")
        self._history = history
        self._changed = changed
        self._retry_interval_s = retry_interval_s
        self._fresh_channel_after = fresh_channel_after

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                # Each stream opens with a notice, covering changes committed while it was down.
                async for _ in self._history.watch_session_summaries():
                    failures = 0
                    self._changed()
                failures = 0
            except ConnectionError, TimeoutError:
                failures += 1
                logger.warning("History Service change watch failed; reconnecting", exc_info=True)
                if failures % self._fresh_channel_after == 0:
                    logger.warning("history watch failed %d times in a row; dialing a fresh channel", failures)
                    await self._history.redial()
                await asyncio.sleep(self._retry_interval_s)
