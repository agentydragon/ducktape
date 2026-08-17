"""What a room is shown while one turn runs: the typing indicator and the status line.

The driver alone — it is handed a frontend and told what the conversation did, and never learns
which room that frontend speaks to, how a line is created and edited, or which backend the events
came off. `channels/matrix/session.py` owns the middle one.

**It reads <conversation_events.py>, not a provider's wire.** Matching here on one backend's own
top-level `type`, `system` subtypes and content blocks would make the channel-neutral driver a
frame interpreter, and leave a second backend's room silent while its agent worked.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Protocol

from haku.console.x.conversation_events import (
    ConversationEvent,
    MessageCompleted,
    Reasoning,
    TextDelta,
    ToolCallStarted,
)

# How long a turn runs before the room is told anything about it. Below this the answer itself is
# the status, and a status/answer pair for a five-second exchange is clutter.
STATUS_AFTER_SECONDS = 8.0

# How often a running turn re-asserts its typing notice. Comfortably inside the homeserver's expiry
# (`channels/matrix/client.py`'s `TYPING_TIMEOUT_MS`, 30s), whose point is to retire the indicator
# when the console dies rather than to blink it off mid-turn.
TYPING_REFRESH_SECONDS = 10.0

# Floor on how often the room's status line is rewritten. Paced for a reader and for Synapse's
# per-room rate limit, not for how fast the agent changes what it is doing.
#
# Here rather than at the send, because a floor and a "what should it say" have to be one decision:
# a sink that silently declines to send inside its own floor loses the state the driver had already
# recorded as shown.
STATUS_EDIT_INTERVAL_SECONDS = 5.0


def coarse_status(events: Sequence[ConversationEvent]) -> str | None:
    """What the room should be told just happened, or None if none of it means anything to it.

    Over a *run* of events rather than one, because a single moment produces several: a tool call
    starting and the message carrying it completing arrive together, and only the more specific of
    them is worth saying. The caller decides what a run is; the turn loop hands it one frame's
    worth.

    Coarse by rule, not by taste: where a tool is named, the backend's own identifier is passed
    through verbatim. There is deliberately no per-tool copy and no mapping table, because both
    would need maintaining every time the tool surface grows.
    """
    if names := [event.tool_name for event in events if isinstance(event, ToolCallStarted)]:
        return f"running {', '.join(names)}"
    # Prose, thinking, or a message that ended: all of it is the agent writing rather than acting,
    # and the room is told no more than that. `MessageCompleted` is in here because a session that
    # streams no deltas produces no `TextDelta` at all, and its answers would otherwise say nothing.
    if any(isinstance(event, TextDelta | Reasoning | MessageCompleted) for event in events):
        return "writing"
    return None


class StatusFrontend(Protocol):
    """The half of a chat frontend this driver drives, declared beside the driver.

    `session_runtime.ChatFrontend` is this plus what the turn loop says for itself, so a frontend
    satisfies both by implementing one port and this driver depends on no runtime.
    """

    async def show_status(self, text: str) -> None: ...

    async def clear_status(self) -> None: ...

    async def set_typing(self, active: bool) -> None: ...


class TurnStatus:
    """Drives what the room shows while one turn runs: the typing indicator and the status line.

    A polled driver rather than a write on every event, because everything that decides whether to
    speak is about elapsed time — the typing notice's expiry, the status line's lazy-creation
    threshold, its edit floor — and a turn can go a long while producing nothing. Events set the
    state; the loop decides when the room hears about it.

    The two differ in when they start. Typing goes on immediately, because "Haku is working on it"
    is worth nothing after the fact; the status line waits for `STATUS_AFTER_SECONDS`.

    A session no frontend is attached to gets the same driver with nothing to drive, rather than a
    `None` the turn loop has to branch on three times.
    """

    def __init__(self, frontend: StatusFrontend | None):
        self._frontend = frontend
        self._state: str | None = None
        # What the room was last told, so an unchanged state is not re-sent every tick.
        self._shown: str | None = None
        self._started = time.monotonic()
        self._shown_at = 0.0
        self._typed_at = 0.0
        self._task: asyncio.Task[None] | None = None

    def note(self, events: Sequence[ConversationEvent]) -> None:
        if (state := coarse_status(events)) is not None:
            self._state = state

    def start(self) -> None:
        if self._frontend is not None:
            self._task = asyncio.create_task(self._run(self._frontend))

    async def _run(self, frontend: StatusFrontend) -> None:
        while True:
            # Refreshed rather than set once: the homeserver expires a typing notice by itself,
            # which is what stops a dead console leaving one stuck on. Well inside
            # `TYPING_TIMEOUT_MS`, so a slow round trip leaves no gap the operator can see.
            if time.monotonic() - self._typed_at >= TYPING_REFRESH_SECONDS:
                self._typed_at = time.monotonic()
                await frontend.set_typing(True)
            # The pace defers rather than drops: a sink that discarded what arrived inside its
            # floor would leave the room reading a stale state until the *next* change, which on a
            # turn settling into one long tool call is the rest of the turn.
            if (
                self._state is not None
                and self._state != self._shown
                and time.monotonic() - self._started >= STATUS_AFTER_SECONDS
                and time.monotonic() - self._shown_at >= STATUS_EDIT_INTERVAL_SECONDS
            ):
                self._shown, self._shown_at = self._state, time.monotonic()
                await frontend.show_status(self._state)
            await asyncio.sleep(1.0)

    async def finish(self) -> None:
        """Stop driving and take both back, on every path out of the turn including failure."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._frontend is not None:
            await self._frontend.set_typing(False)
            await self._frontend.clear_status()
