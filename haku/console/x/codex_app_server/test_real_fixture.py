from pathlib import Path

import pytest_bazel

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome
from haku.console.x.codex_app_server.projection import RecordedFrame
from haku.console.x.codex_app_server.protocol import read_trace, server_messages
from haku.console.x.codex_app_server.testing.fold import whole_capture
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
from util.bazel.runfiles import get_required_path

_FIXTURE = "haku/console/x/codex_app_server/testdata/real_text_command.sanitized.jsonl"
_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)


def _frames() -> tuple[RecordedFrame, ...]:
    source = Path(_FIXTURE)
    path = source if source.exists() else get_required_path(f"ducktape/{_FIXTURE}")
    return tuple(RecordedFrame(record.seq, record.message) for record in server_messages(read_trace(path)))


def test_real_capture_projects_both_observed_turn_lifecycles():
    projection = whole_capture(_frames())

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


if __name__ == "__main__":
    pytest_bazel.main()
