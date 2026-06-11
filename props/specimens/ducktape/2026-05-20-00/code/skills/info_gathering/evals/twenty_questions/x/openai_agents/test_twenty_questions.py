"""Tests for Twenty Questions OpenAI Agents SDK implementation.

Uses a ScriptedModel that implements the agents.models.interface.Model ABC,
returning scripted tool call responses based on the available tools.
Only the LLM is mocked — all real game logic runs unmodified.
"""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest_bazel
from agents import ToolCallOutputItem, set_tracing_disabled
from agents.items import ModelResponse, TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool

from skills.info_gathering.evals.twenty_questions.x.openai_agents.twenty_questions import _run_sim_and_extract

set_tracing_disabled(True)


class ScriptedModel(Model):
    """A fake Model that returns pre-scripted tool call responses."""

    def __init__(self, responses: list[tuple[str, dict[str, object]]]) -> None:
        self._it: Iterator[tuple[str, dict[str, object]]] = iter(responses)

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Tool],
        output_schema: Any,
        handoffs: Any,
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        tool_name, args = next(self._it)
        tool_call_item = {
            "type": "function_call",
            "id": f"call_{tool_name}",
            "call_id": f"call_{tool_name}",
            "name": tool_name,
            "arguments": json.dumps(args),
        }
        return ModelResponse(
            output=cast(list[TResponseOutputItem], [tool_call_item]),
            usage=MagicMock(input_tokens=0, output_tokens=0, total_tokens=0),
            response_id="fake",
        )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Tool],
        output_schema: Any,
        handoffs: Any,
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError


# -- Helper extraction tests (no LLM needed) --


def _make_tool_output_item(output: str) -> ToolCallOutputItem:
    return ToolCallOutputItem(agent=MagicMock(), raw_item={"output": output}, output=output)


def test_extract_answer() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Answered: yes")]
    response, is_correct, is_invalid, _reason = _run_sim_and_extract(result)
    assert response == "yes"
    assert not is_correct
    assert not is_invalid


def test_extract_correct() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Correct!")]
    _response, is_correct, is_invalid, _reason = _run_sim_and_extract(result)
    assert is_correct
    assert not is_invalid


def test_extract_invalid() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Invalid: not a question")]
    _response, is_correct, is_invalid, reason = _run_sim_and_extract(result)
    assert is_invalid
    assert reason == "not a question"
    assert not is_correct


if __name__ == "__main__":
    pytest_bazel.main()
