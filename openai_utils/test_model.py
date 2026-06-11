"""Tests for ResponsesResult.from_sdk output-item parsing."""

from typing import Any, cast

import pytest
import pytest_bazel
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_reasoning_item import Content, ResponseReasoningItem
from pydantic import ValidationError

from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    ReasoningContentItem,
    ReasoningItem,
    ReasoningOutputTextItem,
    ResponsesResult,
)


def test_reasoning_content_item_stays_strict() -> None:
    # ReasoningContentItem is OpenAI-standard "reasoning_text" only; the z.ai GLM
    # "output_text" variant is a separate type, not a widening of this one.
    with pytest.raises(ValidationError):
        ReasoningContentItem.model_validate({"text": "x", "type": "output_text"})
    assert ReasoningOutputTextItem(text="thinking").type == "output_text"


def _glm_sdk_response() -> Response:
    """A Response shaped like glm-4.6's real output through the LiteLLM bridge:
    a reasoning item whose content is mislabeled "output_text", followed by the
    actual answer as a `message` and a `function_call`. model_construct mirrors
    what the SDK hands us at runtime."""
    return Response.model_construct(
        id="resp_1",
        created_at=0.0,
        model="glm-4.6",
        object="response",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        usage=None,
        output=[
            ResponseReasoningItem.model_construct(
                id="rs_1",
                type="reasoning",
                summary=[],
                # The SDK's reasoning Content.type is Literal["reasoning_text"];
                # GLM sends "output_text". cast injects the real wire value past
                # the SDK literal (model_construct already skips validation).
                content=[
                    Content.model_construct(text="The user wants the total apples.", type=cast(Any, "output_text"))
                ],
            ),
            ResponseOutputMessage.model_construct(
                id="msg_1",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText.model_construct(type="output_text", text="The total is 11.", annotations=[])
                ],
            ),
            ResponseFunctionToolCall.model_construct(
                id="fc_1",
                type="function_call",
                call_id="call_1",
                name="multiply",
                arguments='{"a": 2, "b": 4}',
                status="completed",
            ),
        ],
    )


def test_from_sdk_handles_glm_mislabeled_reasoning_with_separate_answer() -> None:
    result = ResponsesResult.from_sdk(_glm_sdk_response())

    assert [type(item).__name__ for item in result.output] == [
        "ReasoningItem",
        "AssistantMessageOut",
        "FunctionCallItem",
    ]

    reasoning = result.output[0]
    assert isinstance(reasoning, ReasoningItem)
    # GLM's "output_text" reasoning content becomes the distinct variant type,
    # leaving ReasoningContentItem untouched.
    assert reasoning.content == [ReasoningOutputTextItem(text="The user wants the total apples.")]

    # The real answer survives as a separate assistant message — not buried in reasoning.
    answer = result.output[1]
    assert isinstance(answer, AssistantMessageOut)
    assert answer.content is not None
    assert answer.content[0].text == "The total is 11."

    call = result.output[2]
    assert isinstance(call, FunctionCallItem)
    assert call.name == "multiply"


def test_from_sdk_keeps_standard_reasoning_text() -> None:
    resp = Response.model_construct(
        id="resp_2",
        created_at=0.0,
        model="gpt-5",
        object="response",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        usage=None,
        output=[
            ResponseReasoningItem.model_construct(
                id="rs_2",
                type="reasoning",
                summary=[],
                content=[Content.model_construct(text="standard cot", type="reasoning_text")],
            )
        ],
    )
    reasoning = ResponsesResult.from_sdk(resp).output[0]
    assert isinstance(reasoning, ReasoningItem)
    assert reasoning.content == [ReasoningContentItem(text="standard cot")]


if __name__ == "__main__":
    pytest_bazel.main()
