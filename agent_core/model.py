"""Model adapters used by the agent loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import CompletionCreateParams
from pydantic import BaseModel

from openai_utils.api_shape import LLMApiShape
from openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionToolParam,
    InputItem,
    InputTextPart,
    InputTokensDetails,
    OpenAIModelProto,
    OutputText,
    OutputTextPart,
    OutputTokensDetails,
    ReasoningContentItem,
    ReasoningItem,
    ResponseOutItem,
    ResponsesRequest,
    ResponsesResult,
    ResponseUsage,
    SystemMessage,
    ToolChoice,
    ToolChoiceFunction,
    UserMessage,
)
from openai_utils.retry import chat_create_with_retries
from openai_utils.types import ReasoningParams


@dataclass(frozen=True)
class AgentModelRequest:
    input: list[InputItem] | str
    instructions: str | None = None
    tools: list[FunctionToolParam] | None = None
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stream: bool = False
    store: bool | None = None
    reasoning: ReasoningParams | None = None
    max_output_tokens: int | None = None

    def to_responses_request(self) -> ResponsesRequest:
        return ResponsesRequest(
            input=self.input,
            instructions=self.instructions,
            tools=self.tools,
            tool_choice=self.tool_choice,
            parallel_tool_calls=self.parallel_tool_calls,
            stream=self.stream,
            store=self.store,
            reasoning=self.reasoning,
            max_output_tokens=self.max_output_tokens,
        )


@dataclass(frozen=True)
class PreparedAgentModelRequest:
    api_shape: LLMApiShape
    request: AgentModelRequest
    wire_body: dict[str, Any]


@dataclass(frozen=True)
class AgentModelResult:
    id: str
    usage: ResponseUsage | None
    output: list[ResponseOutItem]


class AgentModelProto(ABC):
    model: str
    api_shape: LLMApiShape

    @abstractmethod
    def prepare(self, request: AgentModelRequest) -> PreparedAgentModelRequest: ...

    @abstractmethod
    async def sample(self, request: PreparedAgentModelRequest) -> AgentModelResult: ...


@dataclass
class ResponsesAgentModel(AgentModelProto):
    base: OpenAIModelProto
    model: str = ""
    api_shape: LLMApiShape = LLMApiShape.RESPONSES

    def __post_init__(self) -> None:
        self.model = self.base.model

    def prepare(self, request: AgentModelRequest) -> PreparedAgentModelRequest:
        responses_request = request.to_responses_request()
        wire_body = responses_request.model_dump(mode="json", exclude_none=True)
        wire_body["model"] = self.model
        return PreparedAgentModelRequest(api_shape=self.api_shape, request=request, wire_body=wire_body)

    async def sample(self, request: PreparedAgentModelRequest) -> AgentModelResult:
        result = await self.base.responses_create(request.request.to_responses_request())
        return _agent_result_from_responses(result)


@dataclass
class ChatCompletionsAgentModel(AgentModelProto):
    client: AsyncOpenAI
    model: str
    api_shape: LLMApiShape = LLMApiShape.CHAT_COMPLETIONS

    def prepare(self, request: AgentModelRequest) -> PreparedAgentModelRequest:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _input_to_chat_messages(request.input, request.instructions),
            "stream": False,
        }
        if request.tools is not None:
            body["tools"] = [_tool_to_chat_tool(tool) for tool in request.tools]
        if request.tool_choice is not None:
            body["tool_choice"] = _tool_choice_to_chat_tool_choice(request.tool_choice)
        if request.parallel_tool_calls is not None:
            body["parallel_tool_calls"] = request.parallel_tool_calls
        if request.max_output_tokens is not None:
            body["max_completion_tokens"] = request.max_output_tokens
        return PreparedAgentModelRequest(api_shape=self.api_shape, request=request, wire_body=body)

    async def sample(self, request: PreparedAgentModelRequest) -> AgentModelResult:
        response = await chat_create_with_retries(self.client, cast(CompletionCreateParams, request.wire_body))
        choice = response.choices[0]
        message = choice.message
        output: list[ResponseOutItem] = []

        reasoning_text = _message_extra_str(message, "reasoning_content")
        if reasoning_text:
            output.append(ReasoningItem(content=[ReasoningContentItem(text=reasoning_text)]))

        text = _chat_message_text(message.content)
        if text:
            output.append(AssistantMessageOut(content=[OutputText(text=text)]))

        output.extend(
            [
                FunctionCallItem(
                    name=tool_call.function.name,
                    arguments=tool_call.function.arguments,
                    call_id=tool_call.id,
                    id=tool_call.id,
                )
                for tool_call in message.tool_calls or []
            ]
        )

        return AgentModelResult(id=response.id, usage=_chat_usage_to_response_usage(response.usage), output=output)


def _agent_result_from_responses(result: ResponsesResult) -> AgentModelResult:
    return AgentModelResult(id=result.id, usage=result.usage, output=result.output)


def _content_text(content: Sequence[Any] | None) -> str:
    parts = [
        part.text
        for part in content or []
        if isinstance(part, InputTextPart | OutputTextPart | OutputText) and part.text
    ]
    return "\n".join(parts)


def _chat_message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, BaseModel):
                data = part.model_dump(mode="json")
                if isinstance(data.get("text"), str):
                    parts.append(data["text"])
        return "\n".join(parts)
    return str(content)


def _message_extra_str(message: Any, key: str) -> str | None:
    if not isinstance(message, BaseModel):
        return None
    data = message.model_dump(mode="json")
    value = data.get(key)
    if isinstance(value, str):
        return value
    extra = message.model_extra
    if isinstance(extra, dict):
        extra_value = extra.get(key)
        if isinstance(extra_value, str):
            return extra_value
    return None


def _input_to_chat_messages(input_items: list[InputItem] | str, instructions: str | None) -> list[dict[str, Any]]:
    if isinstance(input_items, str):
        messages: list[dict[str, Any]] = [{"role": "user", "content": input_items}]
    else:
        messages = []
        pending_tool_calls: list[dict[str, Any]] = []

        def flush_tool_calls() -> None:
            nonlocal pending_tool_calls
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": None, "tool_calls": pending_tool_calls})
                pending_tool_calls = []

        for item in input_items:
            if isinstance(item, FunctionCallItem):
                pending_tool_calls.append(
                    {
                        "id": item.call_id,
                        "type": "function",
                        "function": {"name": item.name, "arguments": item.arguments or "{}"},
                    }
                )
                continue

            flush_tool_calls()
            if isinstance(item, UserMessage):
                messages.append({"role": "user", "content": _content_text(item.content)})
            elif isinstance(item, SystemMessage):
                messages.append({"role": "system", "content": _content_text(item.content)})
            elif isinstance(item, AssistantMessage):
                messages.append({"role": "assistant", "content": _content_text(item.content)})
            elif isinstance(item, FunctionCallOutputItem):
                messages.append({"role": "tool", "tool_call_id": item.call_id, "content": item.output})
            elif isinstance(item, ReasoningItem):
                continue
            else:
                raise TypeError(f"Unsupported chat input item: {type(item).__name__}")
        flush_tool_calls()

    if instructions:
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].get("role") == "system":
            insert_at += 1
        messages.insert(insert_at, {"role": "system", "content": instructions})
    return messages


def _tool_to_chat_tool(tool: FunctionToolParam) -> dict[str, Any]:
    function: dict[str, Any] = {"name": tool.name, "parameters": tool.parameters}
    if tool.description is not None:
        function["description"] = tool.description
    if tool.strict is not None:
        function["strict"] = tool.strict
    return {"type": "function", "function": function}


def _tool_choice_to_chat_tool_choice(tool_choice: ToolChoice) -> Any:
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, ToolChoiceFunction):
        return {"type": "function", "function": {"name": tool_choice.name}}
    raise TypeError(f"Unsupported tool_choice: {tool_choice!r}")


def _chat_usage_to_response_usage(usage: Any) -> ResponseUsage | None:
    if usage is None:
        return None
    if not isinstance(usage, BaseModel):
        raise TypeError(f"Unsupported chat usage type: {type(usage).__name__}")
    usage_data = usage.model_dump(mode="json")
    prompt_details = usage_data.get("prompt_tokens_details")
    completion_details = usage_data.get("completion_tokens_details")
    cached_tokens = prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0
    reasoning_tokens = completion_details.get("reasoning_tokens", 0) if isinstance(completion_details, dict) else 0
    return ResponseUsage(
        input_tokens=usage_data["prompt_tokens"],
        output_tokens=usage_data["completion_tokens"],
        total_tokens=usage_data["total_tokens"],
        input_tokens_details=InputTokensDetails(cached_tokens=cached_tokens or 0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning_tokens or 0),
    )
