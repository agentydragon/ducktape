"""The scripted model in the Anthropic Messages dialect Claude Code speaks."""

from __future__ import annotations

import itertools

from x.agentplane.harness_tests.claude import anthropic_sse as sse
from x.agentplane.harness_tests.claude.harness import MODEL
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.model_endpoint import ModelExchange, SseEvent
from x.agentplane.runner.testing.scripted_model import (
    Item,
    ModelRequest,
    Reasoning,
    ScriptedModel,
    ShellCall,
    Text,
    ToolOutput,
)

_signatures = (f"sig_test_{n}" for n in itertools.count(1))


class ClaudeModel(ScriptedModel[MessagesRequest]):
    def __init__(self, endpoint: AnthropicMessages) -> None:
        super().__init__(model=MODEL)
        self.endpoint = endpoint

    async def next_exchange(self) -> ModelExchange[MessagesRequest]:
        return await self.endpoint.await_next_request()

    def parse(self, exchange: ModelExchange[MessagesRequest]) -> ModelRequest[MessagesRequest]:
        request = exchange.request
        return ModelRequest(
            _exchange=exchange,
            model=request.model,
            system_text=request.system_text,
            # Claude Code adds `<system-reminder>` blocks of its own to the user turn on a resumed
            # session; they are harness context, not user input.
            user_texts=[text for text in request.texts("user") if not text.startswith("<system-reminder>")],
            assistant_texts=request.texts("assistant"),
            reasoning_texts=[block.thinking for block in request.thinking_blocks],
            tool_outputs=[ToolOutput(result.tool_use_id, result.text) for result in request.tool_results],
            streaming=request.stream,
        )

    def stream(self, items: list[Item]) -> tuple[SseEvent, ...]:
        return sse.message_stream([_block(item) for item in items], model=self.model).events

    def opened_stream(self) -> tuple[SseEvent, ...]:
        return sse.message_stream([sse.Text("never finished")], model=self.model).through("content_block_start").events


def _block(item: Item) -> sse.Block:
    match item:
        case Text(text):
            return sse.Text(text)
        case Reasoning(text):
            return sse.Thinking(text, next(_signatures))
        case ShellCall(call_id, command):
            return sse.ToolUse(call_id, "Bash", {"command": command})
