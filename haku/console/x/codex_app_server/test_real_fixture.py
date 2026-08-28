from pathlib import Path

import pytest_bazel

from haku.console.chat_models import ItemType, ToolOutcome
from haku.console.conversation.conversation_event import FrameRange, ReasoningDisclosure
from haku.console.x.codex_app_server.projection import RecordedFrame
from haku.console.x.codex_app_server.protocol import read_trace, server_messages
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter
from haku.console.x.codex_app_server.testing.fold import whole_capture
from haku.console.x.conversation_events import (
    CallRef,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnAnswered,
    TurnCompleted,
    TurnFailed,
)
from haku.runtime.x.bridge.protocol import HarnessFrame
from util.bazel.runfiles import get_required_path

_TESTDATA = "haku/console/x/codex_app_server/testdata"
_TEXT_COMMAND = f"{_TESTDATA}/real_text_command.sanitized.jsonl"
_PROVIDER_FAILURE = f"{_TESTDATA}/real_provider_failure.sanitized.jsonl"
_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)


def _frames(fixture: str) -> tuple[RecordedFrame, ...]:
    source = Path(fixture)
    path = source if source.exists() else get_required_path(f"ducktape/{fixture}")
    return tuple(RecordedFrame(record.seq, record.message) for record in server_messages(read_trace(path)))


def test_real_capture_projects_both_observed_turn_lifecycles():
    projection = whole_capture(_frames(_TEXT_COMMAND))

    assert projection.events == (
        MessageStarted(provenance=FrameRange(12, 12)),
        ItemSegment(item=_MESSAGE, text="TRACE", provenance=FrameRange(13, 13)),
        ItemSegment(item=_MESSAGE, text="_TEXT", provenance=FrameRange(14, 14)),
        ItemSegment(item=_MESSAGE, text="_OK", provenance=FrameRange(15, 15)),
        MessageCompleted(backend_item_id="<protocol-id-4>", provenance=FrameRange(12, 16)),
        TurnCompleted(end=TurnAnswered(), provenance=FrameRange(17, 17)),
        ReasoningStarted(provenance=FrameRange(23, 23)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(23, 24)),
        ToolCallStarted(
            call_id="exec-<protocol-id-8>",
            tool_name="commandExecution",
            arguments={"command": "<ABSOLUTE_PATH> -c 'printf TRACE_CMD_OK'", "cwd": "<WORKSPACE>"},
            provenance=FrameRange(25, 25),
        ),
        ToolCallCompleted(
            item=CallRef(call_id="exec-<protocol-id-8>"),
            structured={
                "command": "<ABSOLUTE_PATH> -c 'printf TRACE_CMD_OK'",
                "cwd": "<WORKSPACE>",
                "processId": "<process-id>",
                "source": "unifiedExecStartup",
                "status": "completed",
                "commandActions": [{"type": "unknown", "command": "printf TRACE_CMD_OK"}],
                "exitCode": 0,
                "durationMs": 0,
            },
            outcome=ToolOutcome.SUCCEEDED,
            provenance=FrameRange(26, 26),
        ),
        MessageStarted(provenance=FrameRange(27, 27)),
        ItemSegment(item=_MESSAGE, text="TRACE", provenance=FrameRange(28, 28)),
        ItemSegment(item=_MESSAGE, text="_COMMAND", provenance=FrameRange(29, 29)),
        ItemSegment(item=_MESSAGE, text="_DONE", provenance=FrameRange(30, 30)),
        MessageCompleted(backend_item_id="<protocol-id-9>", provenance=FrameRange(27, 31)),
        TurnCompleted(end=TurnAnswered(), provenance=FrameRange(32, 32)),
    )
    assert projection.unprojected == {}


def _terminal_frame() -> RecordedFrame:
    """The capture's `turn/completed` — the frame that states why the turn failed."""
    return next(frame for frame in _frames(_PROVIDER_FAILURE) if frame.payload.get("method") == "turn/completed")


def _stated_reason(terminal: RecordedFrame) -> str:
    reason = terminal.payload["params"]["turn"]["error"]["message"]
    assert isinstance(reason, str)
    return reason


def test_a_failed_turn_projects_the_reason_the_provider_gave():
    """#4752: a failure reaches the neutral vocabulary in the runtime's own words, not as a bare outcome."""
    terminal = _terminal_frame()

    projected = whole_capture(_frames(_PROVIDER_FAILURE))

    assert projected.events == (
        TurnCompleted(
            end=TurnFailed(reason=_stated_reason(terminal)),
            provenance=FrameRange(terminal.frame_seq, terminal.frame_seq),
        ),
    )


def test_the_reason_the_projection_read_is_the_one_the_loop_is_handed():
    """The adapter composes nothing of its own: what the loop stores is what the frame said."""
    terminal = _terminal_frame()

    effects = (
        CodexRuntimeAdapter()
        .turn_handler()
        .apply(frame_seq=terminal.frame_seq, frame=HarnessFrame(frame=terminal.payload))
    )

    assert effects.completion is not None
    assert effects.completion.end == TurnFailed(reason=_stated_reason(terminal))


def test_the_capture_declares_the_thread_unusable_before_the_turn_ends():
    """Codex states a dead thread separately from a failed turn, and states it first.

    The loop needs both: the turn closes with its reason either way, and only this ends the
    session. The capture is the evidence that the two really are separate frames.
    """
    frames_ = _frames(_PROVIDER_FAILURE)
    handler = CodexRuntimeAdapter().turn_handler()

    effects = [handler.apply(frame_seq=frame.frame_seq, frame=HarnessFrame(frame=frame.payload)) for frame in frames_]

    declared = [index for index, effect in enumerate(effects) if effect.unusable is not None]
    completed = [index for index, effect in enumerate(effects) if effect.completion is not None]
    assert len(declared) == 1
    assert declared < completed


def test_the_retry_notifications_are_still_unread():
    """Codex narrates its own retries and the console projects none of them.

    The operator therefore sees nothing for the minutes Codex spends retrying. Out of scope for
    #4752, which is about the turn's outcome; this states the gap so it is not mistaken for done.
    """
    frames_ = _frames(_PROVIDER_FAILURE)

    projected = whole_capture(frames_)

    assert projected.unprojected["error"] == sum(frame.payload.get("method") == "error" for frame in frames_)


if __name__ == "__main__":
    pytest_bazel.main()
