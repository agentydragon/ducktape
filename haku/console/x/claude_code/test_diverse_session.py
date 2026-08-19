"""What the projection does with one whole session, taken off the wire rather than composed.

<test_projection.py>'s fixtures are written from the census's measurements — one hazard per test,
each frame hand-built to carry it. This file's fixture is a session verbatim: every frame Claude
Code 2.1.233 emitted while writing a file, reading it back, counting its lines, running a command
that fails, and summarising (`testdata/diverse_session.jsonl`, redacted; provenance and counts in
<../../debug/frame_shape_census.md> § A direct capture of one session).

That buys two things a composed fixture cannot, and the tests below are those two:

- **The fold meets frames nobody wrote a case for.** Five classes in this capture are outside both
  `_IGNORED_KINDS` and `_IGNORED_SYSTEM_SUBTYPES`, so the default branch is reached five times in
  138 frames. `test_unprojected_is_exactly_todays_unknown_frame_classes` names them, so the next
  CLI release that adds one fails here instead of quietly incrementing a counter nobody reads.
- **The sequence is the CLI's, not ours.** Parallel tool calls answered out of order, a message
  spanning frames with a tool result inside it, an errored call beside successful ones — all in
  the order the wire produced them.

The capture bounds nothing: one session from one CLI version against one model. It is an existence
proof of shapes, not a distribution — the census is where distributions live.
"""

import json
from collections import Counter
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from haku.console.chat_models import ToolOutcome, TurnOutcome
from haku.console.tools.conversations import MAX_PAGE_BYTES
from haku.console.x.claude_code.projection import RecordedFrame, project_log
from haku.console.x.conversation_events import (
    FrameRange,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    Projection,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from util.bazel.runfiles import get_required_path

_CAPTURE = get_required_path("ducktape/haku/console/x/claude_code/testdata/diverse_session.jsonl")

_RECORDS: list[dict[str, Any]] = [json.loads(line) for line in _CAPTURE.read_text().splitlines()]

# A record's index in the capture is its `frame_seq`, so a sequence quoted by a failure names a
# line of the fixture. The one record that carries no `frame` leaves its index unused rather than
# being renumbered away — see `test_a_stdout_line_is_not_a_frame` for what it is.
_FRAMES = [
    RecordedFrame(frame_seq=index, payload=record["frame"])
    for index, record in enumerate(_RECORDS)
    if "frame" in record
]


@pytest.fixture(scope="module")
def projection() -> Projection:
    """The whole capture folded once. Folding at all is the first thing under test: a frame class
    the adapter has never seen must land in `unprojected`, never raise.

    `project_log` rather than `project`: this fixture is a whole session with nothing after it, and
    since #4149 that is the reader's shape of the reducer — `project` returns the state beside the
    projection, for a caller with more frames coming."""
    return project_log(_FRAMES)


def test_the_capture_folds_to_the_session_it_recorded(projection: Projection):
    """Four answers, each reasoned then written then acted on, and a turn that ended."""
    assert Counter(type(event) for event in projection.events) == {
        ReasoningStarted: 4,
        ReasoningCompleted: 4,
        MessageStarted: 4,
        MessageCompleted: 4,
        # One per message, one per reasoning item, and one per tool result that had prose to show.
        ItemSegment: 12,
        ToolCallStarted: 4,
        ToolCallCompleted: 4,
        TurnCompleted: 1,
    }
    # Every message said something and thought something: the fold read both block types out of
    # the frames that carried them, rather than the text of one message landing on another.
    messages = [event for event in projection.events if isinstance(event, MessageCompleted)]
    assert all(message.backend_item_id for message in messages)
    assert len({message.backend_item_id for message in messages}) == len(messages)

    # The turn ran a command that failed on purpose, and the CLI still called the turn a success —
    # which is why `TurnCompleted` is read off `subtype` and a failing call is not a failing turn.
    assert one(event for event in projection.events if isinstance(event, TurnCompleted)).outcome is TurnOutcome.ANSWERED


def test_a_message_outlives_the_tool_results_inside_it(projection: Projection):
    """The census's split-message hazard, occurring on its own: the model asked for two Bash calls
    in one message, and both answers arrived before the message was done."""
    started = [event for event in projection.events if isinstance(event, ToolCallStarted)]
    messages = [event for event in projection.events if isinstance(event, MessageCompleted)]

    # A message's span covers the frames its prose came from, and the calls asked inside it fall
    # between the message opening and the *next* one — rather than the message having been closed
    # by the first non-`assistant` frame between the two calls, which would leave a call outside
    # every message's reach.
    spans = [event.provenance for event in messages]
    assert all(isinstance(span, FrameRange) for span in spans)
    opened = [span.first_frame_seq for span in spans if isinstance(span, FrameRange)]
    ends = [*opened[1:], _FRAMES[-1].frame_seq]
    for call in started:
        assert isinstance(call.provenance, FrameRange)
        asked = call.provenance.first_frame_seq
        assert any(start <= asked <= end for start, end in zip(opened, ends, strict=True))


def test_parallel_calls_pair_by_id_and_not_by_order(projection: Projection):
    """Both Bash calls were asked for before either was answered, and they came back in the order
    they finished — the reverse. Pairing by position would swap this turn's error onto the call
    that succeeded."""
    started = [event for event in projection.events if isinstance(event, ToolCallStarted)]
    completed = [event for event in projection.events if isinstance(event, ToolCallCompleted)]

    assert {event.call_id for event in started} == {event.item.call_id for event in completed}
    assert [event.call_id for event in started] != [event.item.call_id for event in completed]
    # The command that could not succeed is the one marked failed, and it is the only one.
    failed = one(event for event in completed if event.outcome is ToolOutcome.FAILED)
    assert one(event.tool_name for event in started if event.call_id == failed.item.call_id) == "Bash"


def test_unprojected_is_exactly_todays_unknown_frame_classes(projection: Projection):
    """The point of keeping a whole capture. Each key is a frame class the adapter has no case
    for — none of them in `_IGNORED_KINDS` or `_IGNORED_SYSTEM_SUBTYPES`, so each reaches the
    default branch — and naming them here is what makes the next one a failure rather than a
    counter that silently grows:

    - `active_goal` — the session's current goal, `null` throughout this capture.
    - `autocompact_state` — the compaction window and threshold; absent from `protocol.md`.
    - `system/commands_changed` — the slash-command/skill catalog, sent once at startup.
    - `system/task_summary` — a live status line (`detail`), cleared to `null` at the end of the
      turn; also absent from `protocol.md`.
    - `system/post_turn_summary` — the CLI's own verdict on the turn it just finished.

    `rate_limit_event` occurs in the capture too and is deliberately not here: it is ignored by
    name, which is a decision about the account's state rather than a gap in the fold.
    """
    assert dict(projection.unprojected) == {
        "active_goal": 1,
        "autocompact_state": 1,
        "system/commands_changed": 1,
        "system/task_summary": 2,
        "system/post_turn_summary": 1,
    }


def test_a_stdout_line_is_not_a_frame():
    """Claude Code writes plain prose to stdout, interleaved with the JSON stream — here a warning
    about stdin. A reader that assumes every stdout line parses raises on it before any frame
    class is even consulted, so the splitter and not the projection is where it has to be handled.

    The fixture keeps it as `raw_stdout_line` rather than dropping it: a capture with the
    unparseable line removed is a capture that denies this happens.
    """
    stray = one(record["raw_stdout_line"] for record in _RECORDS if "raw_stdout_line" in record)

    with pytest.raises(json.JSONDecodeError):
        json.loads(stray)


def test_the_largest_frame_still_fits_a_transcript_page():
    """One `system/commands_changed` frame was 35,488 bytes before redaction. `conversations.py`
    budgets a *page* rather than a row, so a frame this size is returned whole and costs the rest
    of its page — but a budget below it would clip the one frame that says which commands exist.
    """
    largest = max(record["original_bytes"] for record in _RECORDS if "original_bytes" in record)

    assert largest < MAX_PAGE_BYTES


if __name__ == "__main__":
    pytest_bazel.main()
