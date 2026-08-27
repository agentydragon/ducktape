"""One paced outbound queue per Matrix room.

Synapse limits how fast a user may send events into a room (`rc_message`), and
<../../../../../cluster/k8s/matrix/app/helmrelease.yaml> does not override it, so the upstream
defaults are the whole budget: a burst of ten sends, refilling at one every five seconds. Every
sender the console has shares it — a turn's answer, the span lines' edits, and the sealed
notices — the bootstrap narration folds into the session's one line rather than sending one per.

**A queue, not a rate limiter.** A limiter makes the caller wait, and the loudest caller is the
sandbox's progress reporter, which runs inside the loop draining the runner's output — blocking it
five seconds a line stalls the socket carrying Claude's own frames. The room falls behind; the
conversation does not.

**FIFO, with a collapsing slot per revisable subject.** An answer, a bootstrap line and a rejection
notice each lose information when dropped, so they queue. A span's line genuinely is state — nobody
needs the tool call it showed four edits ago — so each revisable subject takes a single slot
rewritten in place, keeping the position it was first given rather than jumping the queue on every
change.

**One queue per attachment, addressed through `RoomPacers`.** Everything that sends into a room
runs on the sync leader — its own binding notices, and each attachment's reconciler — so one
process holds one bucket per room. The bucket is still an estimate: Synapse keys `rc_message` by
sender, so N rooms of one bot share the homeserver's real budget, and a leadership change hands
the bucket over unfilled. The homeserver's own correction is a 429's `retry_after_ms`, and
`_penalise` is where that lands — which is also why nio's unlimited in-request 429 retry is
bounded (`client.MAX_RATE_LIMIT_RETRIES`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from haku.console.x.channels.matrix.client import MatrixError

logger = logging.getLogger(__name__)

# Synapse's `rc_message` defaults, unoverridden by this deployment.
SENDS_PER_SECOND = 0.2
SEND_BURST = 10

# A backstop against a room nobody is draining — a homeserver that is down, or a bot whose token
# was revoked — rather than a tuning knob. Far above a turn's worth of narration, so reaching it
# means something is wrong and the log says so.
MAX_QUEUED_SENDS = 200

# How long a shutdown waits for the queue before dropping what is left. A rolling replica has
# usually just queued the answer it spent a turn producing, and losing that is the roll being
# visible in the room. Bounded because at one send per five seconds a full queue cannot drain, and
# a pod that will not exit is worse.
FLUSH_SECONDS = 5.0

# What the queue holds: a send, deferred. A callable rather than a message type because the only
# thing this object decides is *when*; giving the queue a vocabulary would mean teaching it every
# sender's.
Send = Callable[[], Awaitable[None]]


@dataclass
class _Slot:
    """One queued send, mutable so a revisable subject can be rewritten where it stands."""

    send: Send
    # The revisable subject this slot collapses changes for, or None for an ordinary send.
    key: str | None = None


class RoomPacer:
    """Serialise everything the console sends into one room, at a rate the room will take."""

    def __init__(self, *, sends_per_second: float = SENDS_PER_SECOND, burst: int = SEND_BURST):
        self._per_second = sends_per_second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._filled_at = time.monotonic()
        self._queue: deque[_Slot] = deque()
        self._revisable: dict[str, _Slot] = {}
        self._queued = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    def send(self, send: Send) -> bool:
        """Queue something that must arrive, behind everything already waiting."""
        if len(self._queue) >= MAX_QUEUED_SENDS:
            logger.error("Matrix: %d sends already queued for this room; dropping one", MAX_QUEUED_SENDS)
            return False
        self._queue.append(_Slot(send))
        self._queued.set()
        self._idle.clear()
        return True

    async def send_and_wait(self, send: Send) -> None:
        """Queue one required effect and wait until the homeserver accepted or rejected it.

        Conversation reconciliation cannot advance its durable cursor merely because a send is in
        this process's queue: a crash would then lose the effect. The ordinary fire-and-forget
        callers remain isolated from room failures; this path reports them to the reconciler so it
        leaves the cursor in place and retries the same durable source later.
        """
        completed = asyncio.get_running_loop().create_future()

        async def observed() -> None:
            try:
                await send()
            except BaseException as error:
                if not completed.done():
                    completed.set_exception(error)
                raise
            else:
                if not completed.done():
                    completed.set_result(None)

        if not self.send(observed):
            raise RuntimeError("Matrix room send queue is full")
        await completed

    def revise(self, key: str, send: Send) -> None:
        """Queue a revisable subject's change, replacing one of its own that has not gone out yet."""
        if (slot := self._revisable.get(key)) is not None:
            slot.send = send
            return
        self._revisable[key] = slot = _Slot(send, key=key)
        self._queue.append(slot)
        self._queued.set()
        self._idle.clear()

    def drop(self, key: str) -> None:
        """Forget a revisable subject's change still waiting to be sent.

        For retiring a line: a create-then-immediately-redact costs two of the room's ten sends
        to show something for a fraction of a second. A change already being sent has left the
        queue, which is why retiring is queued behind it rather than replacing it.
        """
        if (slot := self._revisable.pop(key, None)) is None:
            return
        self._queue.remove(slot)
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
            # The token first, so a revision arriving during the wait still collapses
            # into its subject's slot rather than finding it already gone.
            await self._take_token()
            slot = self._queue.popleft()
            if slot.key is not None:
                self._revisable.pop(slot.key, None)
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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.flush(), FLUSH_SECONDS)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class RoomPacers:
    """The per-attachment send budgets: one `RoomPacer` per live attachment, made on first use.

    This is what replaced the process-global pacer — the send budget is addressed by the
    attachment, so a second room queues against its own bucket instead of sharing (and silently
    starving on) the first room's. The rates are the registry's so a test can make every room's
    queue unthrottled at once.

    A pacer exists for as long as the registry runs; nothing retires one when its attachment
    detaches, because an idle pacer is a drained queue and an empty bucket refilling.
    """

    def __init__(self, *, sends_per_second: float = SENDS_PER_SECOND, burst: int = SEND_BURST):
        self._sends_per_second = sends_per_second
        self._burst = burst
        self._pacers: dict[UUID, RoomPacer] = {}
        self._stack: AsyncExitStack | None = None

    async def for_attachment(self, attachment_id: UUID) -> RoomPacer:
        """This attachment's queue, started under the registry's own lifetime."""
        if (pacer := self._pacers.get(attachment_id)) is not None:
            return pacer
        assert self._stack is not None, "RoomPacers.run() is not active"
        pacer = RoomPacer(sends_per_second=self._sends_per_second, burst=self._burst)
        # Registered before the await below, so a concurrent caller shares this pacer rather than
        # making a second one; a send queued in that window waits for the drain task to start.
        self._pacers[attachment_id] = pacer
        await self._stack.enter_async_context(pacer.run())
        return pacer

    async def flush(self) -> None:
        """Wait until everything queued on every pacer has been sent."""
        for pacer in list(self._pacers.values()):
            await pacer.flush()

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Hold every pacer's drain task; exiting flushes and stops each in turn."""
        async with AsyncExitStack() as stack:
            self._stack = stack
            try:
                yield
            finally:
                self._stack = None
                self._pacers.clear()
