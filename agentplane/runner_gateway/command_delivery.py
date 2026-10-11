"""Route dispatch attempts to the replica holding a Sandbox's runner connection, and wait on them.

One LISTEN connection per replica carries both directions: `runner_dispatch` wakes the replica that
owns a Sandbox's connection, and `command_outcome` wakes the RPC waiting on an attempt. Waiters
re-read and re-notify on a timer as well, so a lost notification or an owner change mid-attempt
costs at most one recheck interval.
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from uuid import UUID

from sqlalchemy.engine import URL

from agentplane.postgres.listener import PostgresListener
from agentplane.proto import event_log_pb2, runner_control_pb2 as control_pb2
from agentplane.runner_gateway.commands.dispatch import (
    DISPATCH_CHANNEL,
    OUTCOME_CHANNEL,
    CommandKey,
    DispatchStore,
    Refused,
    parse_dispatch,
    parse_outcome,
)
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationUnavailableError

# The generated message stubs import google.protobuf; mypy reads stubs only from direct deps.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


class CommandRefusedError(Exception):
    """The runner did not admit this delivery. The submission stays pending: a stopped harness
    may run again, and an exact retry reopens the attempt."""

    def __init__(self, refusal: control_pb2.CommandRefused) -> None:
        super().__init__(control_pb2.CommandRefused.Reason.Name(refusal.reason))
        self.refusal = refusal


class CommandDelivery:
    def __init__(
        self,
        database_url: URL,
        *,
        dispatches: DispatchStore,
        leases: ConnectionLeases,
        liveness_s: float,
        recheck_s: float = 2,
    ) -> None:
        self.dispatches = dispatches
        self.leases = leases
        self.liveness_s = liveness_s
        self.recheck_s = recheck_s
        self._routes: dict[UUID, asyncio.Queue[CommandKey]] = {}
        self._waiters: dict[CommandKey, set[asyncio.Event]] = {}
        self.listener = PostgresListener(
            database_url,
            channels=(DISPATCH_CHANNEL, OUTCOME_CHANNEL),
            application_name="agentplane-runner-control",
            notified=self._notified,
            invalidated=self._wake_all,
        )

    @contextmanager
    def route(self, sandbox_uid: UUID) -> Iterator[asyncio.Queue[CommandKey]]:
        """Receive this replica's dispatch wakeups for a Sandbox; the newest local connection wins."""
        attempts: asyncio.Queue[CommandKey] = asyncio.Queue()
        self._routes[sandbox_uid] = attempts
        try:
            yield attempts
        finally:
            if self._routes.get(sandbox_uid) is attempts:
                del self._routes[sandbox_uid]

    async def deliver(
        self, *, sandbox_uid: UUID, session_id: UUID, command_id: str, runner_session_id: str, deadline_s: float
    ) -> event_log_pb2.EventEntry:
        """Deliver an accepted submission through one attempt and return the runner's receipt.

        Raises `CommandRefusedError` on a refusal and `TimeoutError` at the deadline, leaving the
        submission pending either way.
        """
        if self.leases.read_live_epoch(sandbox_uid, liveness_s=self.liveness_s) is None:
            raise DestinationUnavailableError("runner control stream not connected")
        key = CommandKey(session_id, command_id)
        await self.dispatches.open(
            key, sandbox_uid=sandbox_uid, runner_session_id=runner_session_id, deadline_s=deadline_s
        )
        changed = asyncio.Event()
        waiters = self._waiters.setdefault(key, set())
        waiters.add(changed)
        try:
            async with asyncio.timeout(deadline_s):
                while True:
                    changed.clear()
                    match await self.dispatches.read_outcome(key):
                        case event_log_pb2.EventEntry() as receipt:
                            return receipt
                        case Refused(refusal):
                            raise CommandRefusedError(refusal)
                    # Repeating the wakeup covers a missed notification and a new connection owner.
                    await self.dispatches.notify_dispatch(sandbox_uid, key)
                    with suppress(TimeoutError):
                        async with asyncio.timeout(self.recheck_s):
                            await changed.wait()
        finally:
            waiters.remove(changed)
            if not waiters:
                del self._waiters[key]

    def _notified(self, channel: str, payload: object) -> None:
        try:
            if channel == DISPATCH_CHANNEL:
                sandbox_uid, key = parse_dispatch(str(payload))
                if (attempts := self._routes.get(sandbox_uid)) is not None:
                    attempts.put_nowait(key)
                return
            key = parse_outcome(str(payload))
        except ValueError:
            # Waiters recheck on their own; an unreadable wakeup only wakes them early.
            logger.warning("Unreadable dispatch notification on %s; waking all waiters", channel)
            self._wake_all()
            return
        for changed in self._waiters.get(key, ()):
            changed.set()

    def _wake_all(self) -> None:
        for waiters in self._waiters.values():
            for changed in waiters:
                changed.set()
