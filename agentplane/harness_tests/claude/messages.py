"""Typed fixture endpoint for the Anthropic Messages wire API."""

from __future__ import annotations

from typing import Any

from agentplane.harness_tests.claude.requests import MessagesRequest
from agentplane.harness_tests.model_endpoint import ModelEndpoint, ModelExchange


class AnthropicMessages(ModelEndpoint[MessagesRequest]):
    """Scripted Anthropic Messages endpoint; no HTTP request objects escape it."""

    def _parse_request(self, value: Any) -> MessagesRequest:
        return MessagesRequest.model_validate(value)

    async def __aenter__(self) -> AnthropicMessages:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def await_next_request(self) -> ModelExchange[MessagesRequest]:
        return await self._next_exchange()
