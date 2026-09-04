"""The choices in the Codex frame models: JSON-RPC classification, named unknowns, and the one
serde quirk on the way out."""

from __future__ import annotations

import json

import pytest
import pytest_bazel

from x.agentplane.native.codex import driver, wire


def test_frames_classify_by_json_rpc_shape() -> None:
    assert isinstance(wire.parse_frame({"id": "r1", "result": {}}), wire.Response)
    request = wire.parse_frame({"id": 7, "method": "item/tool/requestUserInput", "params": {"q": 1}})
    assert isinstance(request, wire.ServerRequest)
    assert request.id == 7
    notification = wire.parse_frame(
        {"method": "thread/status/changed", "params": {"threadId": "t", "status": {"type": "idle"}}}
    )
    assert isinstance(notification, wire.UnknownNotification)
    assert notification.method == "thread/status/changed"
    with pytest.raises(ValueError, match="not a JSON-RPC frame"):
        wire.parse_frame({"type": "something else"})


def test_items_keep_unknown_types_with_their_fields() -> None:
    frame = wire.parse_frame(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t",
                "turnId": "u",
                "item": {"type": "fileChange", "id": "i1", "changes": [], "status": "completed"},
            },
        }
    )
    assert isinstance(frame, wire.ItemCompleted)
    item = frame.params.item
    assert isinstance(item, wire.UnknownItem)
    assert item.id == "i1"
    assert item.model_extra == {"changes": [], "status": "completed"}


def test_command_execution_fields_arrive_in_camel_case() -> None:
    frame = wire.parse_frame(
        {
            "method": "item/completed",
            "params": {
                "threadId": "t",
                "turnId": "u",
                "item": {
                    "type": "commandExecution",
                    "id": "c1",
                    "command": "/bin/bash -lc 'printf hi'",
                    "cwd": "/w",
                    "status": "failed",
                    "aggregatedOutput": "hi",
                    "exitCode": 23,
                    "durationMs": 5,
                },
            },
        }
    )
    assert isinstance(frame, wire.ItemCompleted)
    item = frame.params.item
    assert isinstance(item, wire.CommandExecutionItem)
    assert item.status is wire.CommandExecutionStatus.FAILED
    turn = wire.parse_frame(
        {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u", "status": "paused"}}}
    )
    assert isinstance(turn, wire.TurnCompleted)
    assert turn.params.turn.status == "paused"
    assert item.exit_code == 23
    assert item.aggregated_output == "hi"


def test_outbound_requests_serialize_camel_case_except_user_input_fields() -> None:
    # serde renames the `UserInput` variant, not its fields: `text_elements` stays snake_case
    # while every params key is camelCase.
    start = json.loads(driver.turn_start("r1", thread_id="t", text="hi").model_dump_json(by_alias=True))
    assert start == {
        "method": "turn/start",
        "id": "r1",
        "params": {"threadId": "t", "input": [{"type": "text", "text": "hi", "text_elements": []}]},
    }
    thread = json.loads(driver.thread_start("r2", cwd="/w", model="m", effort="low").model_dump_json(by_alias=True))
    assert thread["params"]["approvalPolicy"] == "never"
    assert thread["params"]["baseInstructions"] == driver.BASE_INSTRUCTIONS
    assert thread["params"]["ephemeral"] is True
    steer = json.loads(driver.steer("r3", thread_id="t", turn_id="u", text="go").model_dump_json(by_alias=True))
    assert steer["params"]["expectedTurnId"] == "u"
    assert json.loads(driver.initialized().model_dump_json(by_alias=True)) == {"method": "initialized"}


def test_thread_start_names_developer_instructions_only_when_it_has_them() -> None:
    bare = json.loads(driver.thread_start("r1", cwd="/w", model="m", effort="low").model_dump_json(by_alias=True))
    assert "developerInstructions" not in bare["params"]
    instructed = json.loads(
        driver.thread_start("r1", cwd="/w", model="m", effort="low", instructions="Stand by.").model_dump_json(
            by_alias=True
        )
    )
    assert instructed["params"]["developerInstructions"] == "Stand by."
    assert instructed["params"]["baseInstructions"] == driver.BASE_INSTRUCTIONS


if __name__ == "__main__":
    pytest_bazel.main()
