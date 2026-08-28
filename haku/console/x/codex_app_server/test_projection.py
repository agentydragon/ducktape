from pathlib import Path

import pytest_bazel

from haku.console.chat_models import ItemType, ToolOutcome
from haku.console.conversation.conversation_event import FrameRange, ReasoningDisclosure
from haku.console.x.codex_app_server.projection import OpenItem, ProjectionState, RecordedFrame
from haku.console.x.codex_app_server.protocol import read_trace, server_messages
from haku.console.x.codex_app_server.testing.fold import in_batches, whole_capture
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
)
from util.bazel.runfiles import get_required_path

_FIXTURE = "haku/console/x/codex_app_server/testdata/schema_derived_turn.synthetic.jsonl"
_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)
_REASONING = OpenRef(item_type=ItemType.REASONING)


def frames() -> tuple[RecordedFrame, ...]:
    source_path = Path(_FIXTURE)
    path = source_path if source_path.exists() else get_required_path(f"ducktape/{_FIXTURE}")
    return tuple(RecordedFrame(record.seq, record.message) for record in server_messages(read_trace(path)))


def test_schema_derived_fixture_projects_the_supported_surface():
    projection = whole_capture(frames())

    assert projection.events == (
        ReasoningStarted(provenance=FrameRange(10, 10)),
        ItemSegment(item=_REASONING, text="Inspecting ", provenance=FrameRange(11, 11)),
        ItemSegment(item=_REASONING, text="the request.", provenance=FrameRange(12, 12)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(10, 13)),
        MessageStarted(provenance=FrameRange(14, 14)),
        ItemSegment(item=_MESSAGE, text="Done", provenance=FrameRange(15, 15)),
        ItemSegment(item=_MESSAGE, text=".", provenance=FrameRange(16, 16)),
        MessageCompleted(backend_item_id="<ITEM_2>", provenance=FrameRange(14, 16)),
        ToolCallStarted(
            call_id="<ITEM_3>",
            tool_name="commandExecution",
            arguments={"command": "printf ok", "cwd": "<WORKSPACE>"},
            provenance=FrameRange(17, 17),
        ),
        ItemSegment(item=CallRef(call_id="<ITEM_3>"), text="ok\n", provenance=FrameRange(18, 18)),
        ToolCallCompleted(
            item=CallRef(call_id="<ITEM_3>"),
            structured={
                "command": "printf ok",
                "cwd": "<WORKSPACE>",
                "processId": None,
                "source": "agent",
                "status": "completed",
                "commandActions": [],
                "exitCode": 0,
                "durationMs": 5,
            },
            outcome=ToolOutcome.SUCCEEDED,
            provenance=FrameRange(19, 19),
        ),
        ToolCallStarted(
            call_id="<ITEM_4>", tool_name="fixture/echo", arguments={"text": "hello"}, provenance=FrameRange(20, 20)
        ),
        ItemSegment(item=CallRef(call_id="<ITEM_4>"), text="tool ok", provenance=FrameRange(22, 22)),
        ToolCallCompleted(
            item=CallRef(call_id="<ITEM_4>"),
            structured={
                "server": "fixture",
                "tool": "echo",
                "status": "completed",
                "appContext": None,
                "pluginId": None,
                "result": {
                    "content": [{"type": "text", "text": "tool ok"}],
                    "structuredContent": {"echoed": True},
                    "_meta": None,
                },
                "error": None,
                "durationMs": 7,
            },
            outcome=ToolOutcome.SUCCEEDED,
            provenance=FrameRange(22, 22),
        ),
        TurnCompleted(end=TurnAnswered(), provenance=FrameRange(25, 25)),
    )
    assert projection.unprojected == {"item/started/futureThing": 1, "future/notification": 1}


def test_every_batching_and_reprojection_of_the_fixture_is_identical():
    native = frames()
    expected = whole_capture(native)
    assert whole_capture(native) == expected

    for split in range(len(native) + 1):
        assert in_batches([native[:split], native[split:]]) == expected


def test_malformed_and_unknown_notifications_fail_softly():
    projection = whole_capture(
        (
            RecordedFrame(1, {"method": "item/started", "params": {"item": {"type": "agentMessage"}}}),
            RecordedFrame(2, {"method": "item/agentMessage/delta", "params": []}),
            RecordedFrame(3, {"method": "brand/new", "params": {"value": 1}}),
        )
    )
    assert projection.events == ()
    assert projection.unprojected == {"item/started/identity": 1, "item/agentMessage/delta/params": 1, "brand/new": 1}


def test_nonterminal_and_duplicate_tool_completions_fail_softly():
    item: dict[str, object] = {
        "type": "commandExecution",
        "id": "call-1",
        "command": "printf ok",
        "cwd": "<WORKSPACE>",
        "processId": None,
        "source": "agent",
        "commandActions": [],
        "aggregatedOutput": None,
        "exitCode": 0,
        "durationMs": 5,
    }
    projection = whole_capture(
        (
            RecordedFrame(1, {"method": "item/started", "params": {"item": {**item, "status": "inProgress"}}}),
            RecordedFrame(2, {"method": "item/completed", "params": {"item": {**item, "status": "inProgress"}}}),
            RecordedFrame(3, {"method": "item/completed", "params": {"item": {**item, "status": "completed"}}}),
            RecordedFrame(4, {"method": "item/completed", "params": {"item": {**item, "status": "completed"}}}),
        )
    )

    assert [type(event) for event in projection.events] == [ToolCallStarted, ToolCallCompleted]
    assert projection.unprojected == {
        "item/completed/commandExecution/status": 1,
        "item/completed/commandExecution/duplicate": 1,
    }


def test_duplicate_tool_completion_stays_a_duplicate_across_batches():
    item = {
        "type": "commandExecution",
        "id": "call-1",
        "command": "printf ok",
        "cwd": "<WORKSPACE>",
        "status": "completed",
        "exitCode": 0,
    }
    state, first = ProjectionState().advance(
        (
            RecordedFrame(1, {"method": "item/started", "params": {"item": item}}),
            RecordedFrame(2, {"method": "item/completed", "params": {"item": item}}),
        )
    )
    state, second = state.advance((RecordedFrame(3, {"method": "item/completed", "params": {"item": item}}),))

    assert [type(event) for event in first.events] == [ToolCallStarted, ToolCallCompleted]
    assert second.events == ()
    assert second.unprojected == {"item/completed/commandExecution/duplicate": 1}
    assert state.completed_call_ids == frozenset({"call-1"})


def test_adopted_message_without_a_persisted_native_id_continues_the_existing_item():
    state, projected = ProjectionState(open_message=OpenItem(4, 6, None, "half")).advance(
        (
            RecordedFrame(
                7,
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "id": "m1", "text": "half done"}},
                },
            ),
        )
    )

    assert projected.events == (
        ItemSegment(item=_MESSAGE, text=" done", provenance=FrameRange(7, 7)),
        MessageCompleted(backend_item_id="m1", provenance=FrameRange(4, 7)),
    )
    assert state.open_message is None


def test_adopted_reasoning_without_a_persisted_native_id_continues_the_existing_item():
    state, projected = ProjectionState(open_reasoning=OpenItem(4, 6, None, "half")).advance(
        (
            RecordedFrame(
                7,
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "reasoning", "id": "r1", "summary": ["half done"]}},
                },
            ),
        )
    )

    assert projected.events == (
        ItemSegment(item=_REASONING, text=" done", provenance=FrameRange(7, 7)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(4, 7)),
    )
    assert state.open_reasoning is None


def test_open_reasoning_state_carries_delivered_text_across_batches():
    state, first = ProjectionState().advance(
        (
            RecordedFrame(1, {"method": "item/started", "params": {"item": {"type": "reasoning", "id": "r1"}}}),
            RecordedFrame(
                2, {"method": "item/reasoning/summaryTextDelta", "params": {"itemId": "r1", "delta": "half"}}
            ),
        )
    )
    state, second = state.advance(
        (
            RecordedFrame(
                3,
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "reasoning", "id": "r1", "summary": ["half done"]}},
                },
            ),
        )
    )

    assert first.events == (
        ReasoningStarted(provenance=FrameRange(1, 1)),
        ItemSegment(item=_REASONING, text="half", provenance=FrameRange(2, 2)),
    )
    assert second.events == (
        ItemSegment(item=_REASONING, text=" done", provenance=FrameRange(3, 3)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(1, 3)),
    )
    assert state.open_reasoning is None


if __name__ == "__main__":
    pytest_bazel.main()
