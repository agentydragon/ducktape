"""Waiting for something a process outside this one has not done yet.

Every wait is bounded, and every wait can be told what it is waiting on has died: without the
second, a console that exited in its first second still costs the whole budget before the test
says so — and then says it timed out rather than saying what actually happened.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

# Generous, because what is waited for is a container, two processes and a long poll: these tests
# fail on what did not happen rather than on how fast it did not happen. Reaching it means
# something is wedged, and every process's log is in the undeclared outputs.
BUDGET_SECONDS = 180.0


class WedgedError(AssertionError):
    """Something the test was waiting for never happened, or a process it needed died first."""


async def wait_until(
    what: str,
    ready: Callable[[], Awaitable[bool]],
    *,
    check_alive: Callable[[], None] | None = None,
    budget: float = BUDGET_SECONDS,
) -> None:
    """Poll *ready* until it is true, raising `WedgedError` at the budget.

    *check_alive* raises on its own account when whatever is meant to be doing the work has
    already gone, so a wait fails at the death rather than at the deadline. None where nothing
    outside the room can die — a room served by a homeserver alone has no such process.
    """
    deadline = time.monotonic() + budget
    while not await ready():
        if check_alive is not None:
            check_alive()
        if time.monotonic() > deadline:
            raise WedgedError(f"timed out waiting for {what}")
        await asyncio.sleep(0.2)
