"""Wake the app's Thread readers when the History Service fold commits, as the app projector's NOTIFY does."""

import asyncio
import logging

from agentplane.app.changes import Changes
from agentplane.history_service.client import HistoryServiceClient

logger = logging.getLogger(__name__)


class SummaryWatch:
    def __init__(
        self,
        history: HistoryServiceClient,
        changes: Changes,
        *,
        timeout_s: float,
        retry_interval_s: float,
        fresh_channel_after: int = 4,
    ) -> None:
        """`history` is this watch's own client: dialing a fresh channel closes it under any other caller."""
        if timeout_s <= 0 or retry_interval_s <= 0 or fresh_channel_after < 1:
            raise ValueError("timeout, retry interval and fresh-channel threshold must be positive")
        self._history = history
        self._changes = changes
        self._timeout_s = timeout_s
        self._retry_interval_s = retry_interval_s
        self._fresh_channel_after = fresh_channel_after

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                # Each stream opens with a notice, which covers whatever committed while it was down.
                async for _ in self._history.watch_session_summaries(timeout_s=self._timeout_s):
                    failures = 0
                    self._changes.notify()
                failures = 0
            except ConnectionError, TimeoutError:
                failures += 1
                # The chained gRPC error carries the status details the client keeps out of its message.
                logger.warning("History Service summary watch failed; reconnecting", exc_info=True)
                if failures % self._fresh_channel_after == 0:
                    # As in the History Service ingester: a channel can stay UNAVAILABLE across a roll
                    # of the server that a fresh channel reaches at once.
                    logger.warning("summary watch failed %d times in a row; dialing a fresh channel", failures)
                    await self._history.close()
                await asyncio.sleep(self._retry_interval_s)

    async def close(self) -> None:
        await self._history.close()
