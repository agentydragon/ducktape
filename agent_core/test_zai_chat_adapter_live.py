from __future__ import annotations

import json
import os

import pytest_bazel
from openai import AsyncOpenAI

from agent_core.model import AgentModelRequest, ChatCompletionsAgentModel
from openai_utils.api_shape import LLMApiShape
from openai_utils.model import FunctionCallItem, FunctionToolParam, UserMessage


async def test_zai_chat_adapter_tool_call_live() -> None:
    api_key = os.environ.get("ZAI_API_KEY")
    assert api_key, "ZAI_API_KEY must be set for the live z.ai chat adapter smoke test"

    client = AsyncOpenAI(base_url="https://api.z.ai/api/coding/paas/v4", api_key=api_key)
    model = ChatCompletionsAgentModel(client=client, model=os.environ.get("ZAI_MODEL", "glm-4.6"))
    request = AgentModelRequest(
        input=[UserMessage.text('Call record_result with exactly {"answer": "ok"}.')],
        instructions="Use record_result. Do not answer in prose.",
        tools=[
            FunctionToolParam(
                name="record_result",
                description="Record the final answer for the test harness.",
                parameters={
                    "type": "object",
                    "properties": {"answer": {"type": "string", "enum": ["ok"]}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                strict=True,
            )
        ],
        tool_choice="auto",
        max_output_tokens=256,
    )

    prepared = model.prepare(request)
    assert prepared.api_shape == LLMApiShape.CHAT_COMPLETIONS

    try:
        result = await model.sample(prepared)
    finally:
        await client.close()

    tool_calls = [item for item in result.output if isinstance(item, FunctionCallItem)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "record_result"
    arguments = tool_calls[0].arguments
    assert arguments is not None
    assert json.loads(arguments) == {"answer": "ok"}
    assert result.usage is not None
    assert result.usage.total_tokens > 0


if __name__ == "__main__":
    pytest_bazel.main()
