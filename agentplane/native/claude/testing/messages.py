"""Typed fixture endpoint for the Anthropic Messages wire API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agentplane.native.claude.testing.requests import MessagesRequest
from agentplane.native.testing.model_endpoint import ModelEndpoint, ModelExchange


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

    @asynccontextmanager
    async def await_parent_and_child(
        self,
    ) -> AsyncIterator[tuple[ModelExchange[MessagesRequest], ModelExchange[MessagesRequest]]]:
        """Hold the next two requests open as (parent, child). The parent's carries the child launch's
        tool result; arrival order does not say which is which."""
        async with await self.await_next_request() as first, await self.await_next_request() as second:
            yield (first, second) if first.request.tool_results else (second, first)
