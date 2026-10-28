"""The scripted model in the OpenAI Responses dialect Codex speaks."""

from __future__ import annotations

import itertools
import json

from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.requests import InputText, ResponsesRequest
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.model_endpoint import ModelExchange, SseEvent
from agentplane.testing.scripted_model import Item, ModelRequest, Reasoning, ScriptedModel, ShellCall, Text, ToolOutput

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
            effort=(request.reasoning_config or {}).get("effort"),
            system_text="\n".join(
                text
                for text in [request.instructions, *(message.text for message in request.messages("developer"))]
                if text is not None
            ),
            user_texts=[message.text for message in request.messages("user")],
            assistant_texts=[message.text for message in request.messages("assistant")],
            reasoning_texts=[part.text for item in request.reasoning for part in item.summary],
            tool_outputs=[
                ToolOutput(
                    output.call_id,
                    [output.output]
                    if isinstance(output.output, str)
                    else [_text_output(part) for part in output.output],
                )
                for output in [*request.function_call_outputs, *request.custom_tool_call_outputs]
            ],
            tool_calls=[
                *(call.call_id for call in request.function_calls),
                *(call.call_id for call in request.custom_tool_calls),
            ],
            streaming=request.stream,
        )

    def stream(self, request: ModelRequest[ResponsesRequest], items: list[Item]) -> tuple[SseEvent, ...]:
        return responses_sse.response_stream(
            [_item(item, code_mode=request.native.code_mode) for item in items], model=self.model
        ).events

    def opened_stream(self) -> tuple[SseEvent, ...]:
        return (
            responses_sse.response_stream([responses_sse.Message("never finished")], model=self.model)
            .through("response.created")
            .events
        )


def _item(item: Item, *, code_mode: bool) -> responses_sse.Item:
    match item:
        case Text(text):
            return responses_sse.Message(text)
        case Reasoning(text):
            return responses_sse.Reasoning(text, next(_encrypted))
        case ShellCall(call_id, command) if code_mode:
            # Code mode offers no shell tool of its own: a script runs the nested one and emits its output.
            script = f"const result = await tools.exec_command({json.dumps({'cmd': command})});\ntext(result.output);"
            return responses_sse.CustomToolCall(call_id, "exec", script)
        case ShellCall(call_id, command):
            return responses_sse.FunctionCall(call_id, "exec_command", {"cmd": command})


def _text_output(part: object) -> str:
    if not isinstance(part, InputText):
        raise ValueError("CodexModel is a text-only fixture; use OpenAIResponses for image outputs")
    return part.text
