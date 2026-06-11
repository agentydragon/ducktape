#!/usr/bin/env python3
from __future__ import annotations

from typing import Annotated, Any, Literal

from anthropic.types.tool_param import ToolParam
from openai.types.chat import ChatCompletion, CompletionCreateParams as ChatCompletionCreateParams
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.responses import ResponseCreateParams
from pydantic import BaseModel, Field

from openai_utils.model import InputItem, ResponsesResult
from sysrw.anthropic.types import Message as AnthropicMessage, TextBlock

# ------------------------
# Crush (OpenAI Responses)
# ------------------------


class ToolFunction(BaseModel):
    type: Literal["function"] = "function"
    name: str
    description: str | None = None
    # Responses uses input_schema; Chat uses parameters
    input_schema: dict[str, Any] | None = None
    strict: bool | None = None


class CrushWirelogMeta(BaseModel):
    event_type: str | None = None
    path: str | None = None


class CrushSample(BaseModel):
    kind: Literal["crush"] = "crush"
    correlation_id: str | None = None
    timestamp: int | None = None
    oai_request: ResponseCreateParams
    wirelog: CrushWirelogMeta | None = None


# ------------------------
# CCR (Anthropic-style via SDK)
# ------------------------


class Request(BaseModel):
    """Anthropic API request with Pydantic-validated messages.

    Uses adgn.anthropic types (Pydantic) instead of anthropic.types (TypedDicts).
    """

    system: str | list[TextBlock] | None = None
    messages: list[AnthropicMessage]
    tools: list[ToolParam] | None = None


class CCRSample(BaseModel):
    kind: Literal["ccr"] = "ccr"
    correlation_id: str | None = None
    timestamp: int | None = None
    anthropic_request: Request


# ------------------------
# Eval pipeline IO records
# ------------------------


class ChatAssistantMessage(BaseModel):
    """Chat completion assistant message from CCR samples."""

    kind: Literal["chat"] = "chat"
    message: ChatCompletionMessage


class ResponsesAssistantMessage(BaseModel):
    """Responses API assistant message from Crush samples."""

    kind: Literal["responses"] = "responses"
    responses_input: list[InputItem]
    responses_output: ResponsesResult


AssistantMessage = Annotated[ChatAssistantMessage | ResponsesAssistantMessage, Field(discriminator="kind")]


class Grade(BaseModel):
    """Grade result from the grader model."""

    score: int = Field(ge=1, le=5, description="Score from 1 (worst) to 5 (best)")
    rationale: str = Field(description="Explanation of the score")


class EvalSampleRecord(BaseModel):
    """Eval sample with request, response, and grading information.

    Supports both Chat Completions and Responses API formats.
    """

    request: ChatCompletionCreateParams | ResponseCreateParams
    response: ChatCompletion | ResponsesResult
    new_assistant_message: AssistantMessage
    correlation_id: str | None = None
    timestamp: int | None = None
    anthropic_request: Request | None = None
    grade: Grade | None = None


class EvalGradeRecord(BaseModel):
    """Grader's evaluation using Responses API."""

    request: ResponseCreateParams
    response: ResponsesResult
    correlation_id: str | None = None
    timestamp: int | None = None


# Discriminated union for dataset samples
Sample = Annotated[CCRSample | CrushSample, Field(discriminator="kind")]
