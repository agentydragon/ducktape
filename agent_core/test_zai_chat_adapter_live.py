from __future__ import annotations

import json
import os

import pytest
import pytest_bazel
from openai import AsyncOpenAI

from agent_core.model import AgentModelRequest, ChatCompletionsAgentModel
from openai_utils.api_shape import LLMApiShape
from openai_utils.model import FunctionCallItem, FunctionToolParam, UserMessage

_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"


async def _sample_tool_call_arguments(*, parameters: dict, strict: bool, user_text: str) -> dict:
    """Send one tool to z.ai glm-4.6 and return the parsed JSON arguments of its sole tool call.

    Used by the tool-schema recipe tests below to observe how glm-4.6 encodes a structured
    (nested-object) parameter under different parameter-schema shapes.
    """
    api_key = os.environ.get("ZAI_API_KEY")
    assert api_key, "ZAI_API_KEY must be set for the live z.ai tool-schema tests"

    client = AsyncOpenAI(base_url=_ZAI_BASE_URL, api_key=api_key)
    model = ChatCompletionsAgentModel(client=client, model=os.environ.get("ZAI_MODEL", "glm-4.6"))
    request = AgentModelRequest(
        input=[UserMessage.text(user_text)],
        instructions="Use start_critic. Do not answer in prose.",
        tools=[
            FunctionToolParam(
                name="start_critic",
                description="Start a critic run on a single example.",
                parameters=parameters,
                strict=strict,
            )
        ],
        tool_choice="auto",
        max_output_tokens=512,
    )
    prepared = model.prepare(request)
    assert prepared.api_shape == LLMApiShape.CHAT_COMPLETIONS
    try:
        result = await model.sample(prepared)
    finally:
        await client.close()

    tool_calls = [item for item in result.output if isinstance(item, FunctionCallItem)]
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments is not None
    arguments = json.loads(tool_calls[0].arguments)
    assert isinstance(arguments, dict)
    return arguments


# The recommended recipe for a discriminated union: a single concrete object that carries the
# superset of fields with `kind` as an enum. glm-4.6 emits this as a proper nested object.
# (Per-`kind` required fields — e.g. files_hash only for file_set — are enforced server-side.)
_EXAMPLE_OBJECT = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["file_set", "whole_snapshot"]},
        "snapshot_slug": {"type": "string"},
        "files_hash": {"type": "string", "description": "required when kind=file_set"},
    },
    "required": ["kind", "snapshot_slug"],
    "additionalProperties": False,
}

_START_CRITIC_USER_TEXT = (
    "Call start_critic. definition_id=sha256:59142, the example is a file_set on "
    "snapshot_slug crush/2025-08-30-internal_db with files_hash c27fe50118b25dbc185f00315006c37b."
)


def _example_union(combinator: str) -> dict:
    """Build an `anyOf`/`oneOf` discriminated union of the two example object variants.

    Mirrors props' ExampleSpec. glm-4.6 stringifies either combinator — see the canary below.
    """
    return {
        combinator: [
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "const": "whole_snapshot"},
                    "snapshot_slug": {"type": "string"},
                },
                "required": ["kind", "snapshot_slug"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "const": "file_set"},
                    "snapshot_slug": {"type": "string"},
                    "files_hash": {"type": "string"},
                },
                "required": ["kind", "snapshot_slug", "files_hash"],
                "additionalProperties": False,
            },
        ],
        "discriminator": {"propertyName": "kind"},
    }


async def test_zai_object_tool_param_returned_as_object_live() -> None:
    """Working recipe: a single concrete object tool parameter comes back as a nested JSON object.

    glm-4.6 reliably emits a proper object for a concrete object schema (even with a multi-value
    enum discriminator). This is the shape props tool inputs should use (contrast the union canary).
    """
    args = await _sample_tool_call_arguments(
        parameters={
            "type": "object",
            "properties": {"definition_id": {"type": "string"}, "example": _EXAMPLE_OBJECT},
            "required": ["definition_id", "example"],
            "additionalProperties": False,
        },
        strict=True,
        user_text=_START_CRITIC_USER_TEXT,
    )
    example = args["example"]
    assert isinstance(example, dict), f"expected a nested object, got {type(example).__name__}: {example!r}"
    assert example["kind"] == "file_set"
    assert example["snapshot_slug"] == "crush/2025-08-30-internal_db"
    assert example["files_hash"] == "c27fe50118b25dbc185f00315006c37b"


async def test_zai_flat_tool_params_returned_live() -> None:
    """Working recipe (alternative): flat top-level scalar params instead of a nested object.

    With no nesting there is nothing for glm-4.6 to stringify; every field is populated directly.
    """
    args = await _sample_tool_call_arguments(
        parameters={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "example_kind": {"type": "string", "enum": ["file_set", "whole_snapshot"]},
                "example_snapshot_slug": {"type": "string"},
                "example_files_hash": {"type": "string"},
            },
            "required": ["definition_id", "example_kind", "example_snapshot_slug"],
            "additionalProperties": False,
        },
        strict=True,
        user_text=_START_CRITIC_USER_TEXT,
    )
    assert args["example_kind"] == "file_set"
    assert args["example_snapshot_slug"] == "crush/2025-08-30-internal_db"


@pytest.mark.parametrize("combinator", ["anyOf", "oneOf"])
@pytest.mark.xfail(
    reason="glm-4.6 stringifies anyOf/oneOf union object tool params instead of emitting a nested "
    "object (see docs/z_ai_api.md). xfail(strict=False) so a future z.ai fix surfaces as xpass, "
    "signalling the union-flattening workaround can be dropped.",
    strict=False,
)
async def test_zai_union_tool_param_returned_as_object_live(combinator: str) -> None:
    """Canary documenting the live limitation: an anyOf/oneOf union example is stringified.

    glm-4.6 returns the union value as a JSON-encoded string, so args["example"] is a str, not a
    dict — which is exactly what broke critic_dev_optimize run a4cb7710 (every start_critic call
    failed Pydantic's "Input should be a valid dictionary or object" validation).
    """
    args = await _sample_tool_call_arguments(
        parameters={
            "type": "object",
            "properties": {"definition_id": {"type": "string"}, "example": _example_union(combinator)},
            "required": ["definition_id", "example"],
            "additionalProperties": False,
        },
        strict=True,
        user_text=_START_CRITIC_USER_TEXT,
    )
    assert isinstance(args["example"], dict), f"glm-4.6 stringified the {combinator} union: {args['example']!r}"


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
