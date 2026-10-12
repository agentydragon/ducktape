"""The choices in the Codex frame models: JSON-RPC classification, named unknowns, and the one
serde quirk on the way out."""

from __future__ import annotations

import functools
import json
from copy import deepcopy
from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.native.codex import driver, wire


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


def test_a_turn_status_the_model_does_not_name_stays_a_string() -> None:
    turn = wire.parse_frame(
        {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u", "status": "paused"}}}
    )
    assert isinstance(turn, wire.TurnCompleted)
    assert turn.params.turn.status == "paused"


def test_outbound_requests_serialize_camel_case_except_user_input_fields() -> None:
    # serde renames the `UserInput` variant, not its fields: `text_elements` stays snake_case
    # while every params key is camelCase.
    start = json.loads(driver.turn_start("r1", thread_id="t", text="hi").model_dump_json(by_alias=True))
    assert start == {
        "method": "turn/start",
        "id": "r1",
        "params": {"threadId": "t", "input": [{"type": "text", "text": "hi", "text_elements": []}]},
    }


def test_thread_start_names_developer_instructions_only_when_it_has_them() -> None:
    start = functools.partial(driver.thread_start, "r1", cwd="/w", model="m", effort="low", base_instructions="Base.")
    bare = json.loads(start().model_dump_json(by_alias=True))
    assert "developerInstructions" not in bare["params"]
    instructed = json.loads(start(instructions="Stand by.").model_dump_json(by_alias=True))
    assert instructed["params"]["developerInstructions"] == "Stand by."
    assert instructed["params"]["baseInstructions"] == "Base."


@pytest.mark.parametrize(
    "frame",
    [
        {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "completed"}}},
        {"method": "item/completed", "params": {"threadId": "t", "turnId": "u", "item": []}},
        {"id": "r", "error": {"code": "not-an-integer", "message": "bad"}},
    ],
)
def test_malformed_known_frames_do_not_fall_back_to_unknown(frame: dict[str, Any]) -> None:
    original = deepcopy(frame)
    with pytest.raises(ValidationError):
        wire.parse_frame(frame)
    assert frame == original


def test_future_notification_retains_opaque_params() -> None:
    raw: dict[str, Any] = {"method": "future/notification", "params": {"parts": [1, {"text": "opaque"}], "state": None}}
    original = deepcopy(raw)
    parsed = wire.parse_frame(raw)
    assert isinstance(parsed, wire.UnknownNotification)
    assert parsed.params == raw["params"]
    assert raw == original


def test_extra_fields_on_known_frames_do_not_break_consumed_fields() -> None:
    raw: dict[str, Any] = {
        "method": "turn/completed",
        "params": {"threadId": "t", "turn": {"id": "u", "status": "completed", "future_nested": [1, 2]}},
        "future_envelope": {"opaque": True},
    }
    original = deepcopy(raw)
    parsed = wire.parse_frame(raw)
    assert isinstance(parsed, wire.TurnCompleted)
    assert parsed.params.turn.id == "u"
    # The transport retains the original frame; the typed model only promises
    # the fields its consumers read, not lossless serialization of known types.
    assert raw == original


if __name__ == "__main__":
    pytest_bazel.main()
