"""Typed fixture endpoint for the OpenAI Responses wire API."""

from __future__ import annotations

from typing import Any

from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.model_endpoint import ModelEndpoint, ModelExchange


class OpenAIResponses(ModelEndpoint[ResponsesRequest]):
    """Scripted OpenAI Responses endpoint; no HTTP request objects escape it."""

    def _parse_request(self, value: Any) -> ResponsesRequest:
        return ResponsesRequest.model_validate(value)

    async def __aenter__(self) -> OpenAIResponses:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def await_next_request(self) -> ModelExchange[ResponsesRequest]:
        return await self._next_exchange()
