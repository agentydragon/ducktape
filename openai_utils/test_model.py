"""Tests for ResponsesResult.from_sdk output-item parsing."""

from typing import Any, cast

import pytest_bazel
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_reasoning_item import Content, ResponseReasoningItem

from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    ReasoningContentItem,
    ReasoningItem,
    ResponsesResult,
)


def test_reasoning_content_item_accepts_glm_output_text() -> None:
    # z.ai GLM via the LiteLLM Responses bridge tags reasoning content
    # "output_text" instead of "reasoning_text"; the model must accept it.
    assert ReasoningContentItem(text="thinking", type="output_text").type == "output_text"
    assert ReasoningContentItem(text="thinking").type == "reasoning_text"


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
    # The mislabeled "output_text" reasoning content is kept as reasoning.
    assert reasoning.content == [ReasoningContentItem(text="The user wants the total apples.", type="output_text")]

    # The real answer survives as a separate assistant message — not buried in reasoning.
    answer = result.output[1]
    assert isinstance(answer, AssistantMessageOut)
    assert answer.content is not None
    assert answer.content[0].text == "The total is 11."

    call = result.output[2]
    assert isinstance(call, FunctionCallItem)
    assert call.name == "multiply"


if __name__ == "__main__":
    pytest_bazel.main()
