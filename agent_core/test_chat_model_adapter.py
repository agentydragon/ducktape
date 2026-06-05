from __future__ import annotations

import json

import httpx
import pytest_bazel
from openai import AsyncOpenAI

from agent_core.model import AgentModelRequest, ChatCompletionsAgentModel
from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionToolParam,
    ReasoningItem,
    SystemMessage,
    UserMessage,
)


async def test_chat_adapter_prepares_and_parses_tool_call() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "I should call the tool.",
                            "content": "Calling now.",
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {"name": "next_tool", "arguments": '{"ok": true}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 15,
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = AsyncOpenAI(base_url="http://test/v1", api_key="sk-test", http_client=http_client)
        model = ChatCompletionsAgentModel(client=client, model="chat-model")
        request = AgentModelRequest(
            input=[
                SystemMessage.text("static system"),
                UserMessage.text("hello"),
                FunctionCallItem(name="first_tool", arguments='{"x": 1}', call_id="call_1"),
                FunctionCallOutputItem(call_id="call_1", output='{"result": 2}'),
            ],
            instructions="dynamic instructions",
            tools=[
                FunctionToolParam(
                    name="next_tool",
                    description="Next tool",
                    parameters={"type": "object", "properties": {}, "additionalProperties": False},
                    strict=True,
                )
            ],
            tool_choice="required",
            parallel_tool_calls=False,
        )

        prepared = model.prepare(request)
        assert prepared.wire_body["messages"] == [
            {"role": "system", "content": "static system"},
            {"role": "system", "content": "dynamic instructions"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "first_tool", "arguments": '{"x": 1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"result": 2}'},
        ]

        result = await model.sample(prepared)
    finally:
        await http_client.aclose()

    assert captured["body"] == prepared.wire_body
    assert result.id == "chatcmpl_test"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.input_tokens_details is not None
    assert result.usage.input_tokens_details.cached_tokens == 3
    assert result.usage.output_tokens == 5
    assert result.usage.output_tokens_details is not None
    assert result.usage.output_tokens_details.reasoning_tokens == 2
    assert isinstance(result.output[0], ReasoningItem)
    assert isinstance(result.output[1], AssistantMessageOut)
    assert isinstance(result.output[2], FunctionCallItem)
    assert result.output[2].call_id == "call_2"


if __name__ == "__main__":
    pytest_bazel.main()
