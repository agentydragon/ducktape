from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, Self, cast

from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails, ResponseUsage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from adgn.openai_utils.types import ReasoningEffort, ReasoningParams

# ------------------------------
# Typed, tolerant input items we compose into Responses API "input"
# ------------------------------


class InputTextPart(BaseModel):
    type: Literal["input_text"] = "input_text"
    text: str
    model_config = ConfigDict(extra="allow")


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[InputTextPart] | None = None
    model_config = ConfigDict(extra="allow")

    @classmethod
    def text(cls, text: str) -> Self:
        return cls(content=[InputTextPart(text=text)])


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: list[InputTextPart]
    model_config = ConfigDict(extra="allow")

    @classmethod
    def text(cls, text: str) -> Self:
        return cls(content=[InputTextPart(text=text)])


class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: list[InputTextPart]
    model_config = ConfigDict(extra="allow")

    @classmethod
    def text(cls, text: str) -> Self:
        return cls(content=[InputTextPart(text=text)])


class ReasoningSummaryItem(BaseModel):
    """Summary item within a reasoning block."""

    text: str
    type: Literal["summary_text"] = "summary_text"


class ReasoningItem(BaseModel):
    """Our internal reasoning item representation.

    Gets converted to SDK format when sending to API.
    """

    type: Literal["reasoning"] = "reasoning"
    id: str | None = None
    summary: list[ReasoningSummaryItem] = Field(default_factory=list)  # API requires this field
    model_config = ConfigDict(extra="allow")


class FunctionCallItem(BaseModel):
    type: Literal["function_call"] = "function_call"
    name: str
    arguments: str | None = None  # Must be string (JSON) if provided
    call_id: str
    id: str | None = None  # Preserve the function call's unique ID from the API
    status: str | None = None  # Preserve the status field if present
    model_config = ConfigDict(extra="allow")


class FunctionCallOutputItem(BaseModel):
    # Responses API prefers the payload under "output".
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str | None = Field(default=None, description="Tool output as string (JSON if structured)")
    model_config = ConfigDict(extra="allow")


InputItem = AssistantMessage | UserMessage | SystemMessage | ReasoningItem | FunctionCallItem | FunctionCallOutputItem


class ToolChoiceFunction(BaseModel):
    type: Literal["function"] = "function"
    name: str
    model_config = ConfigDict(extra="allow")


ToolChoice = Literal["auto", "required", "none"] | ToolChoiceFunction


class FunctionToolParam(BaseModel):
    """Typed mirror of OpenAI Responses function tool schema.

    Shape compatible with the Responses API tools list items.
    """

    type: Literal["function"] = "function"
    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: bool | None = None


class ResponsesRequest(BaseModel):
    """Thin, tolerant request model for OpenAI Responses API calls we make."""

    input: list[InputItem] | str

    # Common options we actually use; others are passed through (extra=allow)
    instructions: str | None = None
    tools: list[FunctionToolParam] | None = None
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stream: bool = False
    store: bool | None = None
    reasoning: ReasoningParams | None = None
    max_output_tokens: int | None = None

    # Allow unknown fields for forward-compat (timeouts, metadata, etc.)
    model_config = ConfigDict(extra="allow")

    def to_kwargs(self) -> dict[str, Any]:
        """Normalize to kwargs compatible with AsyncOpenAI.responses.create()."""

        def norm_item(x: BaseModel | str | dict[str, Any]) -> dict[str, Any] | str:
            """Normalize an item for OpenAI API compatibility.

            Pydantic models are serialized; other values pass through.

            Args:
                x: BaseModel to serialize, or already-serialized value (str/dict).

            Returns:
                Serialized dict for models, or the original value.
            """
            if isinstance(x, BaseModel):
                return x.model_dump(exclude_none=True)
            return x

        payload = self.model_dump(exclude_none=True)
        input_value = payload.get("input")
        if isinstance(input_value, list):
            payload["input"] = [norm_item(it) for it in input_value]
        return payload


# ------------------------------
# Structured response types (our layer)
# ------------------------------


class OutputText(BaseModel):
    text: str
    annotations: list[dict[str, Any]] | None = None
    model_config = ConfigDict(extra="allow")


class AssistantMessageOut(BaseModel):
    """Adapter-level assistant message output (text parts only for now).

    Matches the SDK's message content shape we actually use: a list of text parts
    with optional annotations. This keeps a stable, Pydantic-validated shape
    for downstream use and can be extended if we support non-text parts later.
    """

    kind: Literal["assistant_message"] = "assistant_message"
    parts: list[OutputText]
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _coerce_text(cls, data: str | dict[str, Any]) -> dict[str, Any]:
        """Coerce various input forms to the standard parts-based format.

        Args:
            data: Either a plain text string, or a dict with "text" key, or already
                  in the standard format with "parts" key.

        Returns:
            Dict with "parts" key containing a list of OutputText-compatible dicts.
        """
        if isinstance(data, str):
            return {"parts": [{"text": data}]}
        if isinstance(data, dict) and "parts" not in data:
            text = data.get("text")
            if isinstance(text, str):
                new_data = dict(data)
                new_data.pop("text", None)
                new_data["parts"] = [{"text": text}]
                return new_data
        return data

    @property
    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if part.text)

    def to_input_item(self) -> AssistantMessage:
        content_parts: list[InputTextPart] = []
        for part in self.parts:
            part_data = part.model_dump(exclude_none=True)
            part_data.setdefault("type", "input_text")
            content_parts.append(InputTextPart.model_validate(part_data))
        return AssistantMessage(role="assistant", content=content_parts)


ResponseOutItem = ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut


def _message_output_to_assistant(message: ResponseOutputMessage) -> AssistantMessageOut | None:
    parts: list[OutputText] = []
    for content_item in message.content:
        if isinstance(content_item, ResponseOutputText):
            part = OutputText(
                text=content_item.text,
                annotations=[annotation.model_dump(exclude_none=True) for annotation in content_item.annotations]
                if content_item.annotations
                else None,
            )
            parts.append(part)
    if not parts:
        return None
    return AssistantMessageOut(parts=parts)


# Removed legacy aliases; use AssistantMessageOut and OutputText explicitly


class ResponsesResult(BaseModel):
    id: str
    usage: ResponseUsage | None
    output: list[ResponseOutItem]


def convert_sdk_response(sdk_resp: Response) -> ResponsesResult:
    """Convert an OpenAI SDK Response to our typed ResponsesResult.

    Mirrors OpenAIModel.responses_create conversion so non-Pydantic clients
    (that accept kwargs) can still be used with MiniCodex.
    """
    out_items: list[ResponseOutItem] = []
    for item in sdk_resp.output:
        if isinstance(item, ResponseReasoningItem):
            # Convert SDK Summary objects to our ReasoningSummaryItem
            summary_items = []
            if item.summary:
                summary_items = [ReasoningSummaryItem(text=s.text, type=s.type) for s in item.summary]
            out_items.append(ReasoningItem(id=item.id, summary=summary_items))
        elif isinstance(item, ResponseFunctionToolCall):
            out_items.append(
                FunctionCallItem(
                    name=item.name,
                    arguments=item.arguments,  # Already string from SDK
                    call_id=item.call_id,
                    id=item.id,
                    status=item.status,
                )
            )
        elif isinstance(item, ResponseOutputMessage):
            converted = _message_output_to_assistant(item)
            if converted is not None:
                out_items.append(converted)
        else:
            continue
    return ResponsesResult(id=sdk_resp.id, usage=sdk_resp.usage, output=out_items)


# ------------------------------
# Thin wrapper used in prod/tests
# ------------------------------


@dataclass
class OpenAIModel:
    client: AsyncOpenAI

    @property
    def responses(self):  # Pydantic-only surface: .responses.create(ResponsesRequest)
        outer = self

        class _Compat:
            async def create(self, req: ResponsesRequest) -> ResponsesResult:
                result = await outer.responses_create(req)
                return cast(ResponsesResult, result)

        return _Compat()

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        """Create a Responses completion (non-streaming) and convert to our types."""
        if not isinstance(req, ResponsesRequest):
            raise TypeError("responses_create expects a ResponsesRequest instance")
        # No baked-in defaults; caller must set model/tool_choice/reasoning explicitly

        kwargs = req.to_kwargs()
        sdk_resp: Response = await self.client.responses.create(**kwargs)
        return convert_sdk_response(sdk_resp)


# ------------------------------
# Test-friendly fake (records typed CapturedRequest, returns canned outputs)
# ------------------------------


@dataclass
class BoundOpenAIModel:
    """AsyncOpenAI adapter that binds a specific model and returns Pydantic results.

    Implements the OpenAIModelProto protocol.
    """

    client: AsyncOpenAI
    model: str
    reasoning_effort: ReasoningEffort | None = None

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        kwargs = req.to_kwargs()
        # Enforce bound-model contract: always use the instance's model
        kwargs["model"] = self.model
        if self.reasoning_effort and "reasoning" not in kwargs:
            kwargs["reasoning"] = {"effort": self.reasoning_effort.value}
        sdk_resp: Response = await self.client.responses.create(**kwargs)
        return convert_sdk_response(sdk_resp)


# ---------------------------------------------
# Protocol for MiniCodex consumption (bound model)
# ---------------------------------------------


class OpenAIModelProto(Protocol):  # pragma: no cover - structural typing only
    @property
    def model(self) -> str: ...

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult: ...
