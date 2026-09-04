"""The choices in the Claude frame models: unknown kinds stay named, and blocks parse the same
inside frames as they do in upstream requests."""

from __future__ import annotations

import json

import pytest_bazel

from x.agentplane.native.claude import driver, wire
from x.agentplane.native.claude.blocks import ToolResultBlock, ToolUseBlock, UnknownBlock, blocks_of


def test_unknown_kinds_decode_to_named_variants_at_every_level() -> None:
    frame = wire.parse_frame({"type": "rate_limit_event", "detail": 1})
    assert isinstance(frame, wire.UnknownFrame)
    assert frame.type == "rate_limit_event"

    event = wire.parse_frame(
        {"type": "stream_event", "event": {"type": "message_stop"}, "session_id": "s", "uuid": "u"}
    )
    assert isinstance(event, wire.StreamEventFrame)
    assert isinstance(event.event, wire.UnknownStreamEvent)

    request = wire.parse_frame(
        {"type": "control_request", "request_id": "r", "request": {"subtype": "request_user_dialog", "x": 1}}
    )
    assert isinstance(request, wire.ControlRequestFrame)
    assert isinstance(request.request, wire.UnknownControlRequest)
    assert request.request.subtype == "request_user_dialog"

    assistant = wire.parse_frame(
        {
            "type": "assistant",
            "message": {"id": "m", "content": [{"type": "server_tool_use", "id": "x"}]},
            "session_id": "s",
            "uuid": "u",
        }
    )
    assert isinstance(assistant, wire.AssistantFrame)
    assert isinstance(assistant.message.content[0], UnknownBlock)

    lifecycle = wire.parse_frame(
        {"type": "command_lifecycle", "command_uuid": "c", "state": "someday", "uuid": "u", "session_id": "s"}
    )
    assert isinstance(lifecycle, wire.CommandLifecycleFrame)
    assert lifecycle.state == "someday"
    cancelled = wire.parse_frame(
        {"type": "command_lifecycle", "command_uuid": "c", "state": "cancelled", "uuid": "u", "session_id": "s"}
    )
    assert isinstance(cancelled, wire.CommandLifecycleFrame)
    assert cancelled.state is wire.CommandState.CANCELLED


def test_tool_result_text_joins_text_blocks_and_skips_the_rest() -> None:
    frame = wire.parse_frame(
        {
            "type": "user",
            "uuid": "u",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "text", "text": "a"},
                            {"type": "image", "source": {}},
                            {"type": "text", "text": "b"},
                        ],
                        "is_error": True,
                    }
                ],
            },
        }
    )
    assert isinstance(frame, wire.UserFrame)
    assert not frame.is_replay
    assert frame.tool_use_result is None
    (block,) = blocks_of(frame.message.content)
    assert isinstance(block, ToolResultBlock)
    assert block.text == "ab"
    assert block.is_error


def test_a_failed_tool_result_is_a_message_not_a_record() -> None:
    frame = wire.parse_frame(
        {
            "type": "user",
            "uuid": "u",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x", "is_error": True}],
            },
            "tool_use_result": "Error: Exit code 23\nfailing",
        }
    )
    assert isinstance(frame, wire.UserFrame)
    assert frame.tool_use_result == "Error: Exit code 23\nfailing"


def test_streamed_tool_use_carries_its_own_id() -> None:
    frame = wire.parse_frame(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
            },
            "session_id": "s",
            "uuid": "u",
        }
    )
    assert isinstance(frame, wire.StreamEventFrame)
    assert isinstance(frame.event, wire.ContentBlockStart)
    assert isinstance(frame.event.content_block, ToolUseBlock)
    assert frame.event.content_block.id == "toolu_1"


def test_outbound_frames_serialize_the_wire_shape_the_harness_reads() -> None:
    user = json.loads(driver.user_frame("hi", message_uuid="fixed").model_dump_json(by_alias=True))
    assert user == {
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "parent_tool_use_id": None,
        "uuid": "fixed",
    }
    interrupt = json.loads(driver.interrupt(cancel_queued=True).model_dump_json(by_alias=True))
    assert interrupt["request"] == {"subtype": "interrupt", "reason": "capture", "cancel_queued": True}
    assert interrupt["request_id"].startswith("capture-")


def test_initialize_names_only_the_options_it_was_given() -> None:
    """An option the CLI's schema reads as `string | null` must be absent, not null, when unset."""
    bare = json.loads(driver.initialize().model_dump_json(by_alias=True))
    assert bare["type"] == "control_request"
    assert bare["request"] == {"subtype": "initialize"}
    hooked = json.loads(driver.initialize(hooks={"Stop": ["cb-1"]}).model_dump_json(by_alias=True))
    assert hooked["request"] == {"subtype": "initialize", "hooks": {"Stop": [{"hookCallbackIds": ["cb-1"]}]}}
    instructed = json.loads(driver.initialize(instructions="Stand by.").model_dump_json(by_alias=True))
    assert instructed["request"] == {"subtype": "initialize", "appendSystemPrompt": "Stand by."}


if __name__ == "__main__":
    pytest_bazel.main()
