"""What a room is shown while one turn runs: the typing indicator and the status line.

The driver alone — it is handed two coroutines and told about frames, and never learns which
room it is speaking to or how a line is created and edited. `matrix_session.py` owns that half,
and a session with no room gets the same driver with the no-op sinks below.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from haku.console.x.session_frames import content_blocks

# How long a turn runs before the room is told anything about it (R6.2). Below this the
# answer itself is the status, and a status/answer pair for a five-second exchange is
# clutter.
STATUS_AFTER_SECONDS = 8.0

# How often a running turn re-asserts its typing notice. Comfortably inside the homeserver's
# expiry (`matrix_client.TYPING_TIMEOUT_MS`, 30s), because the point of that expiry is to retire
# the indicator when the console dies — not to blink it off mid-turn while it is still going.
TYPING_REFRESH_SECONDS = 10.0

# Floor on how often the room's status line is rewritten. Paced for a reader and for Synapse's
# per-room rate limit, not for how fast the agent changes what it is doing.
#
# Here rather than at the send, because a floor and a "what should it say" have to be one
# decision: a sink that silently declines to send inside its own floor loses the state the
# driver had already recorded as shown. This is the driver's to defer, and the eventual
# room-wide pacer takes it over along with every other sender.
STATUS_EDIT_INTERVAL_SECONDS = 5.0


def coarse_status(frame: dict[str, Any]) -> str | None:
    """What the room should be told this frame means, or None if it means nothing to it.

    Coarse by rule, not by taste (R6.3): where a tool is named, the CLI's own identifier is
    passed through verbatim, and where the CLI wrote a human-readable description of a task
    it is used as-is. There is deliberately no per-tool copy and no mapping table, because
    both would need maintaining every time the tool surface grows.
    """
    match frame.get("type"):
        case "assistant":
            names = [block["name"] for block in content_blocks(frame) if block.get("type") == "tool_use"]
            return f"running {', '.join(names)}" if names else "writing"
        case "system":
            match frame.get("subtype"):
                # `description` here is the CLI's own prose for the step in flight, e.g.
                # "Running Count regular files in the directory" — better than anything the
                # console could reconstruct from a tool name and its arguments.
                case "task_started" | "task_progress":
                    return str(frame.get("description") or "working")
    return None


async def ignore_status(text: str) -> None:
    del text


async def ignore_clear() -> None:
    pass


async def ignore_typing(active: bool) -> None:
    del active


class TurnStatus:
    """Drives what the room shows while one turn runs: the typing indicator and the status line.

    A polled driver rather than a write on every frame, because everything that decides whether
    to speak is about elapsed time — the typing notice's expiry, the status line's lazy-creation
    threshold, its edit floor — and a turn can go a long while between frames. Frames set the
    state; the loop decides when the room hears about it.

    The two differ in when they start. Typing goes on immediately, because "Haku is working on
    it" is the whole message and it is worth nothing after the fact; the status line waits for
    `STATUS_AFTER_SECONDS`, because a status/answer pair for a five-second exchange is clutter.
    """

    def __init__(
        self,
        show: Callable[[str], Awaitable[None]],
        clear: Callable[[], Awaitable[None]],
        typing: Callable[[bool], Awaitable[None]] = ignore_typing,
    ):
        self._show = show
        self._clear = clear
        self._typing = typing
        self._state: str | None = None
        # What the room was last told, so an unchanged state is not re-sent every tick. The sync
        # service drops a repeat anyway, but a driver that says the same thing once a second is
        # relying on that rather than meaning it.
        self._shown: str | None = None
        self._started = time.monotonic()
        self._shown_at = 0.0
        self._typed_at = 0.0
        self._task: asyncio.Task[None] | None = None

    def note(self, frame: dict[str, Any]) -> None:
        if (state := coarse_status(frame)) is not None:
            self._state = state

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            # Refreshed rather than set once: the homeserver expires a typing notice by itself,
            # which is what stops a dead console from leaving one stuck on — so a live turn has
            # to keep saying it. Well inside `TYPING_TIMEOUT_MS`, so a slow round trip does not
            # leave a gap the operator can see.
            if time.monotonic() - self._typed_at >= TYPING_REFRESH_SECONDS:
                self._typed_at = time.monotonic()
                await self._typing(True)
            # One owner for the pace, and it defers rather than drops: a sink that discarded what
            # arrived inside its floor would leave the room reading a stale state until the *next*
            # change, which on a turn settling into one long tool call is the rest of the turn.
            if (
                self._state is not None
                and self._state != self._shown
                and time.monotonic() - self._started >= STATUS_AFTER_SECONDS
                and time.monotonic() - self._shown_at >= STATUS_EDIT_INTERVAL_SECONDS
            ):
                self._shown, self._shown_at = self._state, time.monotonic()
                await self._show(self._state)
            await asyncio.sleep(1.0)

    async def finish(self) -> None:
        """Stop driving and take both back, on every path out of the turn including failure."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._typing(False)
        await self._clear()
