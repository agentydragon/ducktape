from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import cast

from openai.types.responses import (
    FunctionTool,
    Response,
    ResponseFunctionToolCall,
    ResponseInputItemParam,
    ResponseReasoningItem,
    Tool,
)
from openai.types.responses.response_reasoning_item import Summary
from pydantic import TypeAdapter
import pytest

from ember.history import ConversationHistory

_INPUT_ITEM_ADAPTER: TypeAdapter[ResponseInputItemParam] = TypeAdapter(ResponseInputItemParam)


@pytest.fixture
def history(tmp_path: Path) -> ConversationHistory:
    return ConversationHistory(tmp_path / "history.jsonl")


def test_history_persists_and_builds_input_items(history: ConversationHistory) -> None:
    user_item = _INPUT_ITEM_ADAPTER.validate_python(
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi there"}]}
    )
    history.append_input(user_item)

    response = _response_with_tool_call(
        call_id="call-1", tool_name="run_shell_command", arguments='{"command": "echo hi"}'
    )
    history.append_response(response)

    function_output = _INPUT_ITEM_ADAPTER.validate_python(
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"exit_code": 0, "stdout": "hi", "stderr": ""}',
        }
    )
    history.append_input(function_output)

    items = [cast(Mapping[str, object], item) for item in history.build_input_items("system prompt")]

    assert items[0]["role"] == "system"
    reasoning_items = [item for item in items if item.get("type") == "reasoning"]
    assert reasoning_items
    reasoning_model = ResponseReasoningItem.model_validate(reasoning_items[0])
    assert reasoning_model.encrypted_content == "ciphertext"
    assert reasoning_model.content in (None, [])

    function_outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert function_outputs, "No function call outputs recorded"
    assert function_outputs[0]["call_id"] == "call-1"

    lines = history.path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert sum(1 for record in records if record["input_item"] is not None) == 2
    assert any(record["response"] is not None for record in records)

    reloaded = ConversationHistory(history.path)
    reloaded_items = [cast(Mapping[str, object], entry) for entry in reloaded.build_input_items("system prompt")]
    assert len(reloaded_items) == len(items)
    assert [item.get("type") for item in reloaded_items] == [item.get("type") for item in items]


def _response_with_tool_call(call_id: str, tool_name: str, arguments: str) -> Response:
    reasoning = ResponseReasoningItem(
        id="reasoning-1",
        type="reasoning",
        summary=[Summary(type="summary_text", text="thinking")],
        content=[],
        encrypted_content="ciphertext",
    )

    function_call = ResponseFunctionToolCall(call_id=call_id, name=tool_name, arguments=arguments, type="function_call")

    tools = cast(
        list[Tool],
        [
            FunctionTool(
                name="run_shell_command",
                description="Execute shell command.",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                strict=False,
                type="function",
            ),
            FunctionTool(
                name="sleep_until_user_message",
                description="Suspend agent until a new user message arrives.",
                parameters={"type": "object", "properties": {}},
                strict=False,
                type="function",
            ),
        ],
    )

    return Response(
        id="resp-test",
        created_at=0.0,
        model="gpt-5",
        object="response",
        output=[reasoning, function_call],
        parallel_tool_calls=False,
        tool_choice="required",
        tools=tools,
    )
