"""The admin port's change stream: a wakeup at each commit of decisions, from any replica."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.engine import URL

from agentplane.egress.decision_store import DECISIONS_CHANNEL
from agentplane.postgres.wakeups import WakeupListener


class DecisionChanges:
    def __init__(self, database_url: URL, *, heartbeat_s: float) -> None:
        self._database_url = database_url
        self._heartbeat_s = heartbeat_s

    async def follow(self) -> AsyncIterator[None]:
        """Yields on subscribing, so a reader re-reads what committed before it listened, then at each
        commit and after every `heartbeat_s` of quiet, which tells the reader the stream is alive.
        Ends when the LISTEN connection is lost; the reader subscribes again.

        One connection per follower, opened here: the proxy's admission must not depend on a database
        that is down when it starts, and a follower finds that out on its own request.
        """
        wakeups = WakeupListener(
            self._database_url, channels=[DECISIONS_CHANNEL], application_name="agentplane-egress-decision-changes"
        )
        committed = asyncio.Event()
        async with wakeups.listener.connection():
            with wakeups.changes[DECISIONS_CHANNEL].subscribe(committed):
                # Losing the connection wakes the channel like a commit, which ends the loop.
                while wakeups.listener.connected:
                    committed.clear()
                    yield
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(committed.wait(), self._heartbeat_s)
