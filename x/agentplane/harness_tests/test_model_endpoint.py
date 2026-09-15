"""Regression tests for the typed scripted model endpoint."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
import pytest_bazel

from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.harness_tests.model_endpoint import JsonResponse, ModelRequestParseError, SseEvent


async def test_invalid_native_request_fails_the_waiting_script_without_exposing_raw_http() -> None:
    async with AnthropicMessages() as messages, aiohttp.ClientSession() as client:
        response = await client.post(messages.origin, json={"not": "an Anthropic Messages request"})
        assert response.status == 400

        with pytest.raises(ModelRequestParseError, match="typed model fixture rejected a native request"):
            await messages.await_next_request()


async def test_sse_response_is_rejected_for_a_non_streaming_typed_request() -> None:
    async with AnthropicMessages() as messages, aiohttp.ClientSession() as client:
        request = asyncio.create_task(
            client.post(
                messages.origin,
                json={"model": "test", "stream": False, "system": "", "messages": [], "thinking": {"type": "disabled"}},
            )
        )
        async with await messages.await_next_request() as exchange:
            with pytest.raises(RuntimeError, match="cannot send SSE to a non-streaming model request"):
                await exchange.send(SseEvent(kind="message", data=b"data: {}\n\n"))
            await exchange.respond(JsonResponse(b"{}"))
        response = await request
        assert response.status == 200


if __name__ == "__main__":
    pytest_bazel.main()
