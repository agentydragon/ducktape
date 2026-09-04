"""The scripted model in the OpenAI Responses dialect Codex speaks."""

from __future__ import annotations

import itertools

from x.agentplane.harness_tests.codex import responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL
from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream, Stream, UpstreamRequest
from x.agentplane.runner.testing.scripted_model import (
    Item,
    ModelRequest,
    Reasoning,
    ScriptedModel,
    ShellCall,
    Text,
    ToolOutput,
)

_encrypted = (f"enc_test_{n}" for n in itertools.count(1))


class CodexModel(ScriptedModel):
    def __init__(self, upstream: ScriptedUpstream) -> None:
        super().__init__(upstream, model=MODEL)

    def parse(self, raw: UpstreamRequest) -> ModelRequest:
        request = ResponsesRequest.parse(raw)
        return ModelRequest(
            raw=raw,
            system_text="\n".join([request.instructions, *(message.text for message in request.messages("developer"))]),
            user_texts=[message.text for message in request.messages("user")],
            assistant_texts=[message.text for message in request.messages("assistant")],
            reasoning_texts=[part.text for item in request.reasoning for part in item.summary],
            tool_outputs=[ToolOutput(output.call_id, output.output) for output in request.function_call_outputs],
            streaming=request.stream,
        )

    def stream(self, items: list[Item]) -> Stream:
        return sse.response_stream([_item(item) for item in items], model=self.model)

    def opened_stream(self) -> Stream:
        return sse.response_stream([sse.Message("never finished")], model=self.model).until("response.created").held()


def _item(item: Item) -> sse.Item:
    match item:
        case Text(text):
            return sse.Message(text)
        case Reasoning(text):
            return sse.Reasoning(text, next(_encrypted))
        case ShellCall(call_id, command):
            return sse.FunctionCall(call_id, "exec_command", {"cmd": command})
