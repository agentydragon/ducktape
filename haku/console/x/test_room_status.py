"""What the room is shown while a turn runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest_bazel

from haku.console.x.room_status import (
    STATUS_AFTER_SECONDS,
    STATUS_EDIT_INTERVAL_SECONDS,
    TYPING_REFRESH_SECONDS,
    TurnStatus,
    coarse_status,
)


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def test_a_tool_call_becomes_a_status_naming_the_tool_verbatim() -> None:
    """R6.3: the CLI's own identifier, with no per-tool copy to maintain."""
    frame = _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}})

    assert coarse_status(frame) == "running Bash"


def test_a_task_frame_reuses_the_description_the_cli_already_wrote() -> None:
    frame = {"type": "system", "subtype": "task_progress", "description": "Running the test suite"}

    assert coarse_status(frame) == "Running the test suite"


def test_frames_the_room_has_no_use_for_produce_no_status() -> None:
    assert coarse_status({"type": "result", "subtype": "success"}) is None
    assert coarse_status({"type": "system", "subtype": "commands_changed"}) is None


async def test_a_short_turn_leaves_no_status_behind() -> None:
    """R6.2: below the threshold the answer is the status, and a pair of them is clutter."""
    shown: list[str] = []
    status = TurnStatus(_appender(shown), _noop)
    status.start()
    status.note(_assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}))
    await asyncio.sleep(1.2)
    await status.finish()

    assert shown == []


async def test_a_slow_turn_says_what_it_is_doing_and_then_retires_the_line() -> None:
    shown: list[str] = []
    cleared: list[bool] = []

    async def clear() -> None:
        cleared.append(True)

    status = TurnStatus(_appender(shown), clear)
    status._started -= STATUS_AFTER_SECONDS  # the turn has already been running a while
    status.start()
    status.note(_assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}))
    await asyncio.sleep(1.2)
    await status.finish()

    assert shown == ["running Bash"]
    assert cleared == [True]


async def test_a_state_that_changes_inside_the_floor_is_deferred_rather_than_lost() -> None:
    """The floor may delay what the room is told; it may not decide the room is never told it.

    This used to be the sink's: it declined silently inside its own edit interval while this
    driver had already recorded the state as shown, so a turn that changed tools twice in five
    seconds left the room reading the first of them — until the *next* change, which on a turn
    that then settles into one long tool call is the rest of the turn.
    """
    shown: list[str] = []

    status = TurnStatus(_appender(shown), _noop)
    status._started -= STATUS_AFTER_SECONDS  # the turn has already been running a while
    status.start()
    status.note(_assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}))
    await asyncio.sleep(1.2)
    status.note(_assistant({"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}))
    await asyncio.sleep(1.2)

    assert shown == ["running Read"], "the second change lands inside the floor"

    status._shown_at -= STATUS_EDIT_INTERVAL_SECONDS  # the floor passes
    await asyncio.sleep(1.2)
    await status.finish()

    assert shown == ["running Read", "running Bash"], "and is still waiting when it does"


async def test_the_line_is_retired_even_when_the_turn_fails() -> None:
    """A line still saying \"running Bash\" after the turn died is the stuck-indicator bug."""
    cleared: list[bool] = []

    async def clear() -> None:
        cleared.append(True)

    status = TurnStatus(_appender([]), clear)
    status.start()
    await status.finish()

    assert cleared == [True]


async def test_typing_starts_with_the_turn_rather_than_waiting_for_the_status_threshold() -> None:
    """R6.1: "Haku is working on it" is worth nothing after the fact, so unlike the status line
    it does not wait — a turn shorter than `STATUS_AFTER_SECONDS` still shows it."""
    typed: list[bool] = []

    status = TurnStatus(_appender([]), _noop, _recorder(typed))
    status.start()
    await asyncio.sleep(1.2)
    await status.finish()

    assert typed == [True, False], "on at the start, off at the end, and no status line in between"


async def test_typing_is_refreshed_for_the_length_of_the_turn() -> None:
    """The homeserver expires the notice on its own — which is what keeps a dead console from
    leaving one stuck on — so a live turn has to keep saying it."""
    typed: list[bool] = []

    status = TurnStatus(_appender([]), _noop, _recorder(typed))
    status._typed_at -= TYPING_REFRESH_SECONDS  # the last notice is already due for renewal
    status.start()
    await asyncio.sleep(1.2)

    assert typed == [True]
    status._typed_at -= TYPING_REFRESH_SECONDS
    await asyncio.sleep(1.2)
    await status.finish()

    assert typed == [True, True, False]


async def test_typing_is_taken_back_even_when_the_turn_fails() -> None:
    """The stuck typing indicator this requirement is named after: every terminal path clears it,
    failure included, and `finish()` is the one hook all of them run."""
    typed: list[bool] = []

    status = TurnStatus(_appender([]), _noop, _recorder(typed))
    status.start()
    await status.finish()

    assert typed[-1] is False


def _recorder(sink: list[bool]) -> Callable[[bool], Awaitable[None]]:
    async def typing(active: bool) -> None:
        sink.append(active)

    return typing


def _appender(sink: list[str]) -> Callable[[str], Awaitable[None]]:
    async def show(text: str) -> None:
        sink.append(text)

    return show


async def _noop() -> None:
    pass


if __name__ == "__main__":
    pytest_bazel.main()
