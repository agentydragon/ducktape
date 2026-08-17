"""What the room is shown while a turn runs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest_bazel

from haku.console.chat_models import TurnOutcome
from haku.console.x.claude_code.projection import DeltaSource, RecordedFrame, project_log
from haku.console.x.claude_code.testing.wire import (
    assistant,
    result,
    system,
    text_block,
    text_delta,
    thinking_block,
    tool_result,
    tool_use_block,
)
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    ConversationEvent,
    FrameRange,
    MessageCompleted,
    MessageKey,
    Outcome,
    Reasoning,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
)
from haku.console.x.room_status import (
    STATUS_AFTER_SECONDS,
    STATUS_EDIT_INTERVAL_SECONDS,
    TYPING_REFRESH_SECONDS,
    TurnStatus,
    coarse_status,
)


class _RecordingFrontend:
    """A frontend that keeps what the driver told it, in place of a room."""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self.cleared = 0
        self.typed: list[bool] = []

    async def show_status(self, text: str) -> None:
        self.shown.append(text)

    async def clear_status(self) -> None:
        self.cleared += 1

    async def set_typing(self, active: bool) -> None:
        self.typed.append(active)


_WHERE = FrameRange(1, 1)
_MESSAGE = MessageKey(opened_at_frame_seq=1)


def _tool_call(name: str) -> ToolCallStarted:
    return ToolCallStarted(message=_MESSAGE, call_id=f"call-{name}", tool_name=name, arguments={}, provenance=_WHERE)


def _message_completed() -> MessageCompleted:
    return MessageCompleted(message=_MESSAGE, text="hello", agent_message_id="msg-1", provenance=_WHERE)


def test_a_tool_call_becomes_a_status_naming_the_tool_verbatim() -> None:
    """R6.3: the backend's own identifier, with no per-tool copy to maintain."""
    assert coarse_status([_tool_call("Bash")]) == "running Bash"


def test_a_message_completing_beside_its_tool_call_does_not_bury_the_tool() -> None:
    """One frame's events, and the more specific of them wins — otherwise the room reads "writing"
    for the whole of a turn that is running tools."""
    assert coarse_status([_tool_call("Bash"), _message_completed()]) == "running Bash"


def test_an_activity_reuses_the_description_the_harness_already_wrote() -> None:
    activity = ActivityStarted(
        activity_id="task-1", call_id="toolu_1", description="Running the test suite", provenance=_WHERE
    )

    assert coarse_status([activity]) == "Running the test suite"


def test_prose_and_thinking_are_both_just_writing() -> None:
    """A session that streams no deltas produces only the completed message, and one that streams
    produces the deltas — the room is told the same thing either way."""
    assert coarse_status([TextDelta(message=_MESSAGE, text="hel", provenance=_WHERE)]) == "writing"
    assert coarse_status([Reasoning(message=_MESSAGE, summary=None, provenance=_WHERE)]) == "writing"
    assert coarse_status([_message_completed()]) == "writing"


def test_events_the_room_has_no_use_for_produce_no_status() -> None:
    finished: list[ConversationEvent] = [
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=_WHERE),
        ActivityCompleted(activity_id="task-1", summary=None, outcome=Outcome.SUCCEEDED, provenance=_WHERE),
    ]

    assert coarse_status(finished) is None
    assert coarse_status([]) is None


def test_a_claude_turn_still_reads_the_way_it_did_off_the_frames() -> None:
    """The one test here that names a backend, and the only place the claim can be made.

    `room_status.py` used to match on Claude's own `type`, `subtype`s and content blocks; this is
    what it read then, projected through the adapter and read as events now. The frames are the
    census's shapes (<../debug/frame_shape_census.md>: one content block per `assistant` frame), and
    the cut is `_run_turn`'s — one frame, `STREAM_EVENTS`, fresh state — so what this asserts is
    exactly the sequence a room sees.
    """
    frames: list[tuple[dict[str, Any], str | None]] = [
        (assistant(thinking_block("hm"), message_id="msg_A"), "writing"),
        (assistant(text_block("Looking."), message_id="msg_A"), "writing"),
        (assistant(tool_use_block("toolu_1", "Bash", {"command": "ls"}), message_id="msg_A"), "running Bash"),
        (system("task_started", task_id="task_9", tool_use_id="toolu_1", description="npm run build"), "npm run build"),
        (tool_result("toolu_1", "ok"), None),
        (text_delta("A"), "writing"),
        (result(), None),
    ]

    assert [
        coarse_status(
            project_log([RecordedFrame(frame_seq=seq, payload=payload)], delta_source=DeltaSource.STREAM_EVENTS).events
        )
        for seq, (payload, _) in enumerate(frames)
    ] == [expected for _, expected in frames]


async def test_a_short_turn_leaves_no_status_behind() -> None:
    """R6.2: below the threshold the answer is the status, and a pair of them is clutter."""
    frontend = _RecordingFrontend()
    status = TurnStatus(frontend)
    status.start()
    status.note([_tool_call("Bash")])
    await asyncio.sleep(1.2)
    await status.finish()

    assert frontend.shown == []


async def test_a_turn_with_no_frontend_drives_nothing_and_still_finishes() -> None:
    """The turn loop does not learn which surface it is on: a session attached to no chat frontend
    gets this same driver, with nothing to drive and nothing to take back at the end."""
    status = TurnStatus(None)
    status.start()
    status.note([_tool_call("Bash")])
    await asyncio.sleep(1.2)
    await status.finish()

    assert status._task is None, "nothing to poll, so nothing polls"


async def test_a_slow_turn_says_what_it_is_doing_and_then_retires_the_line() -> None:
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status._started -= STATUS_AFTER_SECONDS  # the turn has already been running a while
    status.start()
    status.note([_tool_call("Bash")])
    await asyncio.sleep(1.2)
    await status.finish()

    assert frontend.shown == ["running Bash"]
    assert frontend.cleared == 1


async def test_a_state_that_changes_inside_the_floor_is_deferred_rather_than_lost() -> None:
    """The floor may delay what the room is told; it may not decide the room is never told it.

    This used to be the sink's: it declined silently inside its own edit interval while this
    driver had already recorded the state as shown, so a turn that changed tools twice in five
    seconds left the room reading the first of them — until the *next* change, which on a turn
    that then settles into one long tool call is the rest of the turn.
    """
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status._started -= STATUS_AFTER_SECONDS  # the turn has already been running a while
    status.start()
    status.note([_tool_call("Read")])
    await asyncio.sleep(1.2)
    status.note([_tool_call("Bash")])
    await asyncio.sleep(1.2)

    assert frontend.shown == ["running Read"], "the second change lands inside the floor"

    status._shown_at -= STATUS_EDIT_INTERVAL_SECONDS  # the floor passes
    await asyncio.sleep(1.2)
    await status.finish()

    assert frontend.shown == ["running Read", "running Bash"], "and is still waiting when it does"


async def test_the_line_is_retired_even_when_the_turn_fails() -> None:
    """A line still saying \"running Bash\" after the turn died is the stuck-indicator bug."""
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status.start()
    await status.finish()

    assert frontend.cleared == 1


async def test_typing_starts_with_the_turn_rather_than_waiting_for_the_status_threshold() -> None:
    """R6.1: "Haku is working on it" is worth nothing after the fact, so unlike the status line
    it does not wait — a turn shorter than `STATUS_AFTER_SECONDS` still shows it."""
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status.start()
    await asyncio.sleep(1.2)
    await status.finish()

    assert (frontend.typed, frontend.shown) == ([True, False], []), "on at the start, off at the end"


async def test_typing_is_refreshed_for_the_length_of_the_turn() -> None:
    """The homeserver expires the notice on its own — which is what keeps a dead console from
    leaving one stuck on — so a live turn has to keep saying it."""
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status._typed_at -= TYPING_REFRESH_SECONDS  # the last notice is already due for renewal
    status.start()
    await asyncio.sleep(1.2)

    assert frontend.typed == [True]
    status._typed_at -= TYPING_REFRESH_SECONDS
    await asyncio.sleep(1.2)
    await status.finish()

    assert frontend.typed == [True, True, False]


async def test_typing_is_taken_back_even_when_the_turn_fails() -> None:
    """The stuck typing indicator this requirement is named after: every terminal path clears it,
    failure included, and `finish()` is the one hook all of them run."""
    frontend = _RecordingFrontend()

    status = TurnStatus(frontend)
    status.start()
    await status.finish()

    assert frontend.typed[-1] is False


if __name__ == "__main__":
    pytest_bazel.main()
