"""What the wake watcher makes of the CLI's idle-time frames, pinned by recorded captures.

Both captures were recorded from a real CLI (2.1.241) run with the bridge's own launch arguments:
one prompt starts work the harness later observes on its own, and every frame after the first
`result` arrived with nothing on stdin — the harness waking itself. The watcher's contract is that
it stays silent through the announcement chatter and fires exactly once, on the frame that begins
the exchange, carrying the best description the chatter offered.
"""

from __future__ import annotations

import json
from typing import Any

import pytest_bazel

from haku.console.x.claude_code.projection import RecordedFrame
from haku.console.x.claude_code.testing.fold import whole_capture
from haku.console.x.claude_code.testing.wire import prompt, tool_result
from haku.console.x.claude_code.wake import GENERIC_WAKE_DESCRIPTION, ClaudeWakeWatcher
from haku.console.x.conversation_events import TurnCompleted
from haku.runtime.x.bridge.protocol import HarnessFrame
from util.bazel.runfiles import get_required_path


def _capture(name: str) -> list[dict[str, Any]]:
    path = get_required_path(f"ducktape/haku/console/x/claude_code/testdata/{name}")
    return [json.loads(line)["frame"] for line in path.read_text().splitlines()]


def _idle_frames(capture: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The frames after the first `result` — what arrives while no turn is open."""
    boundary = next(index for index, frame in enumerate(capture) if frame.get("type") == "result")
    return capture[boundary + 1 :]


def _wakes(idle: list[dict[str, Any]]) -> tuple[int, str]:
    """Feed the idle frames in order; the index the watcher fired at, and its description."""
    watcher = ClaudeWakeWatcher()
    for index, frame in enumerate(idle):
        if (wake := watcher.observe(HarnessFrame(frame=frame))) is not None:
            return index, wake.description
    raise AssertionError("the watcher never fired")


def test_a_background_tasks_completion_wakes_with_its_notifications_summary() -> None:
    capture = _capture("background_wake.jsonl")
    idle = _idle_frames(capture)
    fired_at, description = _wakes(idle)
    notification = next(frame for frame in idle if frame.get("subtype") == "task_notification")
    assert description == notification["summary"]
    # Everything before the firing frame is announcement chatter, and the firing frame is the
    # exchange's first content.
    assert all(frame.get("type") in ("system", "command_lifecycle") for frame in idle[:fired_at])
    assert idle[fired_at]["type"] == "stream_event"


def test_a_scheduled_wakeup_fires_with_the_generic_description() -> None:
    """The `ScheduleWakeup` shape announces itself with a bare `command_lifecycle` — no frame
    carries prose about why, so the wake says the one thing that is true."""
    idle = _idle_frames(_capture("scheduled_wakeup_fire.jsonl"))
    fired_at, description = _wakes(idle)
    assert description == GENERIC_WAKE_DESCRIPTION
    assert idle[fired_at]["type"] == "stream_event"


def test_both_captures_fold_to_two_completed_turns() -> None:
    """The wake exchange is an ordinary exchange to the fold: each capture reads back as two
    turns, so bracketing the second one live asks nothing new of the projection."""
    for name in ("background_wake.jsonl", "scheduled_wakeup_fire.jsonl"):
        frames = [RecordedFrame(frame_seq=index, payload=frame) for index, frame in enumerate(_capture(name))]
        projection = whole_capture(frames)
        completions = [event for event in projection.events if isinstance(event, TurnCompleted)]
        assert len(completions) == 2, name


def test_an_injected_user_command_wakes_with_its_own_text() -> None:
    """Not in the captures: production CLI builds have injected the wake as a visible `user`
    command (session e639eb6f's frame log), so an idle user frame with prose is a wake carrying
    that prose."""
    watcher = ClaudeWakeWatcher()
    wake = watcher.observe(HarnessFrame(frame=prompt("Background task done; take a look.")))
    assert wake is not None
    assert wake.description == "Background task done; take a look."


def test_an_idle_tool_result_user_frame_is_not_a_wake() -> None:
    watcher = ClaudeWakeWatcher()
    assert watcher.observe(HarnessFrame(frame=tool_result("toolu_1", "late output"))) is None


if __name__ == "__main__":
    pytest_bazel.main()
