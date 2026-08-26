from pathlib import Path

import pytest_bazel

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome
from haku.console.x.codex_app_server.projection import RecordedFrame, project_log
from haku.console.x.codex_app_server.protocol import read_trace, server_messages
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter
from haku.console.x.conversation_events import (
    CallRef,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
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
    projection = project_log(_frames(_TEXT_COMMAND))

    assert projection.events == (
        MessageStarted(provenance=FrameRange(12, 12)),
        ItemSegment(item=_MESSAGE, text="TRACE", provenance=FrameRange(13, 13)),
        ItemSegment(item=_MESSAGE, text="_TEXT", provenance=FrameRange(14, 14)),
        ItemSegment(item=_MESSAGE, text="_OK", provenance=FrameRange(15, 15)),
        MessageCompleted(backend_item_id="<protocol-id-4>", provenance=FrameRange(12, 16)),
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(17, 17)),
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
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(32, 32)),
    )
    assert projection.unprojected == {}


def test_provider_failure_capture_projects_only_a_bare_failed_outcome():
    """#4752: the projection keeps the outcome and drops every durable trace of the reason.

    The capture's `error` notifications and its `turn.error` all state why the turn failed, and
    `docs/protocol_evidence.md` reads that shape off them; none of it reaches a durable event.
    """
    frames = _frames(_PROVIDER_FAILURE)

    projected = project_log(frames)

    assert projected.events == (TurnCompleted(outcome=TurnOutcome.FAILED, provenance=FrameRange(27, 27)),)
    assert projected.unprojected["error"] == sum(frame.payload.get("method") == "error" for frame in frames)


def test_provider_failure_reason_survives_only_as_far_as_the_transient_completion():
    """The adapter does read the reason off `turn.error`; no durable conversation event can hold it."""
    completed = next(frame for frame in _frames(_PROVIDER_FAILURE) if frame.payload.get("method") == "turn/completed")

    effects = (
        CodexRuntimeAdapter()
        .turn_handler()
        .apply(frame_seq=completed.frame_seq, frame=HarnessFrame(frame=completed.payload))
    )

    assert effects.completion is not None
    assert (
        effects.completion.failure
        == f"the agent's turn failed: {completed.payload['params']['turn']['error']['message']}"
    )
    assert effects.events == (TurnCompleted(outcome=TurnOutcome.FAILED, provenance=FrameRange(27, 27)),)


if __name__ == "__main__":
    pytest_bazel.main()
