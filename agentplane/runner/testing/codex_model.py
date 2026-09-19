"""The scripted model in the OpenAI Responses dialect Codex speaks."""

from __future__ import annotations

import itertools

from agentplane.harness_tests.codex import responses_sse as sse
from agentplane.harness_tests.codex.harness import MODEL
from agentplane.harness_tests.codex.requests import ResponsesRequest
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.harness_tests.model_endpoint import ModelExchange, SseEvent
from agentplane.runner.testing.scripted_model import (
    Item,
    ModelRequest,
    Reasoning,
    ScriptedModel,
    ShellCall,
    Text,
    ToolOutput,
)

_encrypted = (f"enc_test_{n}" for n in itertools.count(1))


class CodexModel(ScriptedModel[ResponsesRequest]):
    def __init__(self, endpoint: OpenAIResponses) -> None:
        super().__init__(model=MODEL)
        self.endpoint = endpoint

    async def next_exchange(self) -> ModelExchange[ResponsesRequest]:
        return await self.endpoint.await_next_request()

    def parse(self, exchange: ModelExchange[ResponsesRequest]) -> ModelRequest[ResponsesRequest]:
        request = exchange.request
        return ModelRequest(
            _exchange=exchange,
            model=request.model,
            system_text="\n".join([request.instructions, *(message.text for message in request.messages("developer"))]),
            user_texts=[message.text for message in request.messages("user")],
            assistant_texts=[message.text for message in request.messages("assistant")],
            reasoning_texts=[part.text for item in request.reasoning for part in item.summary],
            tool_outputs=[ToolOutput(output.call_id, output.output) for output in request.function_call_outputs],
            streaming=request.stream,
        )

    def stream(self, items: list[Item]) -> tuple[SseEvent, ...]:
        return sse.response_stream([_item(item) for item in items], model=self.model).events

    def opened_stream(self) -> tuple[SseEvent, ...]:
        return sse.response_stream([sse.Message("never finished")], model=self.model).through("response.created").events


def _item(item: Item) -> sse.Item:
    match item:
        case Text(text):
            return sse.Message(text)
        case Reasoning(text):
            return sse.Reasoning(text, next(_encrypted))
        case ShellCall(call_id, command):
            return sse.FunctionCall(call_id, "exec_command", {"cmd": command})
