"""One paced outbound queue per Matrix room.

Synapse limits how fast a user may send events into a room (`rc_message`), and
<../../../cluster/k8s/matrix/app/helmrelease.yaml> does not override it — so the upstream
defaults are the whole budget: a burst of ten sends, refilling at one every five seconds.
Every sender the console has shares that one budget: a turn's answer, the status line's
edits, lifecycle notices, and the bootstrap narration, which is the loudest of them at one
notice per line.

**A queue, not a rate limiter.** A limiter makes the caller wait, and the loudest caller is
the sandbox's progress reporter — which runs inside the loop draining the runner's output,
so blocking it five seconds a line stalls the socket carrying Claude's own frames. Queueing
decouples the two: the room falls behind, the conversation does not.

**FIFO, with one collapsing slot.** A debounce is only correct for latest-wins state. An
answer, a bootstrap line and a `holding N message(s)` each lose information when dropped, so
they queue. The status line is the one thing that genuinely is state — nobody needs the tool
call it showed four edits ago — so it takes a single slot that is rewritten in place, keeping
the position it was first given rather than jumping the queue on every change.

**Per replica, not per room globally.** The sync leader and the replica holding a session's
lease need not be the same pod, so two of these can exist for one room, each believing it owns
the whole budget. The bucket is therefore an estimate. The homeserver's own correction is a
429's `retry_after_ms`, and `_penalise` is where that lands — which is also why nio's
unlimited in-request 429 retry is bounded (`client.MAX_RATE_LIMIT_RETRIES`): a 429 has
to reach this object to be learned from.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from haku.console.x.channels.matrix.client import MatrixError

logger = logging.getLogger(__name__)

# Synapse's `rc_message` defaults, unoverridden by this deployment.
SENDS_PER_SECOND = 0.2
SEND_BURST = 10

# A backstop against a room nobody is draining — a homeserver that is down, or a bot whose
# token was revoked — rather than a tuning knob. It is far above a turn's worth of narration,
# so reaching it means something is wrong and the log says so.
MAX_QUEUED_SENDS = 200

# How long a shutdown waits for the queue before dropping what is left. A rolling replica has
# usually just queued the answer it spent a turn producing and the redaction retiring its
# status line, and losing those is the roll being visible in the room. Bounded because at one
# send per five seconds a full queue cannot drain, and a pod that will not exit is worse.
FLUSH_SECONDS = 5.0

# What the queue holds: a send, deferred. A callable rather than a message type because the
# only thing this object decides is *when* — every sender already knows how to say its own
# thing, and giving the queue a vocabulary would mean teaching it all of them.
Send = Callable[[], Awaitable[None]]


@dataclass
class _Slot:
    """One queued send, mutable so the status line can be rewritten where it stands."""

    send: Send


class RoomPacer:
    """Serialise everything the console sends into one room, at a rate the room will take."""

    def __init__(self, *, sends_per_second: float = SENDS_PER_SECOND, burst: int = SEND_BURST):
        self._per_second = sends_per_second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._filled_at = time.monotonic()
        self._queue: deque[_Slot] = deque()
        self._status: _Slot | None = None
        self._queued = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    def send(self, send: Send) -> None:
        """Queue something that must arrive, behind everything already waiting."""
        if len(self._queue) >= MAX_QUEUED_SENDS:
            logger.error("Matrix: %d sends already queued for this room; dropping one", MAX_QUEUED_SENDS)
            return
        self._queue.append(_Slot(send))
        self._queued.set()
        self._idle.clear()

    def set_status(self, send: Send) -> None:
        """Queue a status-line change, replacing one that has not gone out yet."""
        if self._status is not None:
            self._status.send = send
            return
        self._status = slot = _Slot(send)
        self._queue.append(slot)
        self._queued.set()
        self._idle.clear()

    def drop_status(self) -> None:
        """Forget a status change still waiting to be sent.

        For retiring the line: a create-then-immediately-redact costs two of the room's ten
        sends to show something for a fraction of a second. A change already being sent is
        past this — it has left the queue — which is why retiring is queued behind it rather
        than replacing it.
        """
        if self._status is None:
            return
        self._queue.remove(self._status)
        self._status = None
        if not self._queue:
            self._queued.clear()
            self._idle.set()

    async def _take_token(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._filled_at) * self._per_second)
            self._filled_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._per_second)

    def _penalise(self, seconds: float) -> None:
        """Believe the homeserver's 429 over our own accounting.

        `_filled_at` moves into the future, so the next refill computes a negative elapsed and
        the bucket goes below empty — which is the point: the wait is what the server asked
        for, and only then does the normal refill start again.
        """
        self._tokens = 0.0
        self._filled_at = time.monotonic() + seconds

    async def _drain(self) -> None:
        while True:
            await self._queued.wait()
            # The token first, so a status change arriving during the wait still collapses
            # into its slot rather than finding it already gone.
            await self._take_token()
            slot = self._queue.popleft()
            if slot is self._status:
                self._status = None
            if not self._queue:
                self._queued.clear()
            try:
                await slot.send()
            except Exception as error:
                # Loud rather than raised: this task is the room's, and a room that cannot be
                # spoken to is not a reason to end the conversation happening behind it.
                logger.exception("Matrix: a queued send failed")
                if isinstance(error, MatrixError) and error.retry_after_ms is not None:
                    self._penalise(error.retry_after_ms / 1000)
            finally:
                if not self._queue:
                    self._idle.set()

    async def flush(self) -> None:
        """Wait until everything queued has been sent."""
        await self._idle.wait()

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        task = asyncio.create_task(self._drain(), name="matrix-room-pacer")
        try:
            yield
        finally:
            # A replica shutting down has usually just queued the thing a whole turn produced,
            # so it is given a bounded chance to arrive before the task carrying it is cancelled.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.flush(), FLUSH_SECONDS)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
