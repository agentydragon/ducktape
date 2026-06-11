from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import singledispatch
from typing import Any, Literal, Self, cast

from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    response_usage as sdk_usage_types,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from pydantic import BaseModel, ConfigDict, Field

from openai_utils.errors import translate_context_length
from openai_utils.types import ReasoningEffort, ReasoningParams, build_reasoning_params

# ------------------------------
# Usage types (wrapped from OpenAI SDK)
# ------------------------------


class InputTokensDetails(BaseModel):
    """Token usage details for input (wrapped from OpenAI SDK)."""

    cached_tokens: int
    model_config = ConfigDict(extra="allow")


class OutputTokensDetails(BaseModel):
    """Token usage details for output (wrapped from OpenAI SDK)."""

    reasoning_tokens: int
    model_config = ConfigDict(extra="allow")


class ResponseUsage(BaseModel):
    """Response usage information (wrapped from OpenAI SDK)."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_tokens_details: InputTokensDetails
    output_tokens_details: OutputTokensDetails
    model_config = ConfigDict(extra="allow")

    @classmethod
    def from_sdk(cls, sdk_usage: sdk_usage_types.ResponseUsage) -> Self:
        """Convert OpenAI SDK ResponseUsage to our wrapped type."""
        return cls(
            input_tokens=sdk_usage.input_tokens,
            output_tokens=sdk_usage.output_tokens,
            total_tokens=sdk_usage.total_tokens,
            input_tokens_details=InputTokensDetails(cached_tokens=sdk_usage.input_tokens_details.cached_tokens),
            output_tokens_details=OutputTokensDetails(
                reasoning_tokens=sdk_usage.output_tokens_details.reasoning_tokens
            ),
        )


# ------------------------------
# Typed, tolerant input items we compose into Responses API "input"
# ------------------------------


class InputTextPart(BaseModel):
    type: Literal["input_text"] = "input_text"
    text: str
    model_config = ConfigDict(extra="allow")


class OutputTextPart(BaseModel):
    """Text content from assistant messages.

    When assistant messages appear in the input (multi-turn conversations),
    they use type='output_text' not 'input_text'.
    """

    type: Literal["output_text"] = "output_text"
    text: str
    model_config = ConfigDict(extra="allow")


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[OutputTextPart] | None = None
    # When an assistant message follows a reasoning item, OpenAI requires id
    id: str | None = None
    model_config = ConfigDict(extra="allow")

    @classmethod
    def text(cls, text: str) -> Self:
        return cls(content=[OutputTextPart(text=text)])


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


class ReasoningContentItem(BaseModel):
    """Content item within a reasoning block (contains actual reasoning text)."""

    text: str
    type: Literal["reasoning_text"] = "reasoning_text"


class ReasoningItem(BaseModel):
    """Our internal reasoning item representation.

    Note: status is not included when serializing for input - OpenAI treats it as
    output-only metadata even though the Param type allows it.
    """

    type: Literal["reasoning"] = "reasoning"
    id: str | None = None
    summary: list[ReasoningSummaryItem] = Field(default_factory=list)
    content: list[ReasoningContentItem] = Field(default_factory=list)
    # Don't serialize status for input - use Field(exclude=True) or model_dump(exclude={'status'})
    model_config = ConfigDict(extra="allow")


class FunctionCallItem(BaseModel):
    type: Literal["function_call"] = "function_call"
    name: str
    arguments: str | None = None  # Must be string (JSON) if provided
    call_id: str
    id: str | None = None  # Preserve the function call's unique ID from the API
    status: str | None = None  # Preserve the status field if present
    model_config = ConfigDict(extra="allow")


# ------------------------------
# Function call output content types (Pydantic wrappers for OpenAI types)
# ------------------------------


class FunctionOutputTextContent(BaseModel):
    """Text content in function call output (sent to OpenAI API as input_text)."""

    type: Literal["input_text"] = "input_text"
    text: str
    model_config = ConfigDict(extra="allow")


class FunctionOutputImageContent(BaseModel):
    """Image content in function call output (sent to OpenAI API as input_image)."""

    type: Literal["input_image"] = "input_image"
    image_url: str | None = None
    file_id: str | None = None
    detail: Literal["low", "high", "auto"] | None = None
    model_config = ConfigDict(extra="allow")


FunctionCallOutputContent = FunctionOutputTextContent | FunctionOutputImageContent
FunctionCallOutputType = str | list[FunctionCallOutputContent]


class FunctionCallOutputItem(BaseModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: FunctionCallOutputType
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

    When an assistant message follows a reasoning item, OpenAI requires the message
    id when sending back as input (to link the message to the reasoning item).
    """

    kind: Literal["assistant_message"] = "assistant_message"
    parts: list[OutputText]
    id: str | None = None  # Message ID from SDK response (required for reasoning continuation)
    model_config = ConfigDict(extra="allow")

    @property
    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if part.text)

    def to_input_item(self) -> AssistantMessage:
        content_parts: list[OutputTextPart] = []
        for part in self.parts:
            part_data = part.model_dump()
            part_data.setdefault("type", "output_text")
            content_parts.append(OutputTextPart.model_validate(part_data))
        # When following a reasoning item, OpenAI requires id
        return AssistantMessage(role="assistant", content=content_parts, id=self.id)


ResponseOutItem = ReasoningItem | FunctionCallItem | FunctionCallOutputItem | AssistantMessageOut


@singledispatch
def response_out_item_to_input(item: BaseModel) -> InputItem:
    raise TypeError(f"Unsupported response item type: {type(item)!r}")


def _identity(item: InputItem) -> InputItem:
    return item


response_out_item_to_input.register(ReasoningItem)(_identity)
response_out_item_to_input.register(FunctionCallItem)(_identity)
response_out_item_to_input.register(FunctionCallOutputItem)(_identity)


@response_out_item_to_input.register
def _(item: AssistantMessageOut) -> InputItem:
    return item.to_input_item()


def _message_output_to_assistant(message: ResponseOutputMessage) -> AssistantMessageOut:
    parts = [
        OutputText(
            text=content_item.text,
            annotations=[annotation.model_dump() for annotation in content_item.annotations]
            if content_item.annotations
            else None,
        )
        for content_item in message.content
        if isinstance(content_item, ResponseOutputText)
    ]
    if not parts:
        raise ValueError("ResponseOutputMessage has no text parts")
    # Preserve id - OpenAI requires it when the message follows a reasoning item
    return AssistantMessageOut(parts=parts, id=message.id)


class ResponsesResult(BaseModel):
    id: str
    usage: ResponseUsage | None
    output: list[ResponseOutItem]

    @classmethod
    def from_sdk(cls, sdk_resp: Response) -> Self:
        """Convert an OpenAI SDK Response to our typed ResponsesResult."""
        out_items: list[ResponseOutItem] = []
        for item in sdk_resp.output:
            if isinstance(item, ResponseReasoningItem):
                out_items.append(
                    ReasoningItem(
                        id=item.id,
                        summary=[ReasoningSummaryItem(text=s.text, type=s.type) for s in item.summary]
                        if item.summary
                        else [],
                        content=[ReasoningContentItem(text=c.text, type=c.type) for c in item.content]
                        if item.content
                        else [],
                        # Don't include status - it causes "Unknown parameter" error when sent back as input
                    )
                )
            elif isinstance(item, ResponseFunctionToolCall):
                out_items.append(
                    FunctionCallItem(
                        name=item.name, arguments=item.arguments, call_id=item.call_id, id=item.id, status=item.status
                    )
                )
            elif isinstance(item, ResponseOutputMessage):
                out_items.append(_message_output_to_assistant(item))
            else:
                raise NotImplementedError(f"Unsupported output item type: {type(item)}")
        usage = ResponseUsage.from_sdk(sdk_resp.usage) if sdk_resp.usage else None
        return cls(id=sdk_resp.id, usage=usage, output=out_items)


# ------------------------------
# Protocol for bound model interface
# ------------------------------


class OpenAIModelProto(ABC):
    """Abstract base class for AsyncOpenAI adapters with bound model."""

    model: str

    @abstractmethod
    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult: ...


@dataclass
class BoundOpenAIModel(OpenAIModelProto):
    """AsyncOpenAI adapter that binds a specific model and returns Pydantic results."""

    client: AsyncOpenAI
    model: str
    reasoning_effort: ReasoningEffort | None = None

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        kwargs = req.model_dump()
        kwargs["model"] = self.model
        if self.reasoning_effort and "reasoning" not in kwargs:
            kwargs["reasoning"] = build_reasoning_params(effort=self.reasoning_effort)

        create: Callable[..., Awaitable[Response]] = cast(
            Callable[..., Awaitable[Response]], self.client.responses.create
        )
        sdk_resp: Response = await translate_context_length(create, **kwargs)
        return ResponsesResult.from_sdk(sdk_resp)
