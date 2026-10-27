"""Verify the OpenAI SDK owns retries and bounds them at three requests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest
import pytest_bazel

from openai_utils.client_factory import get_async_openai
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import BoundOpenAIModel, ResponsesRequest
from openai_utils.requests import create_chat_completion


@pytest.fixture(autouse=True)
def skip_sdk_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai.AsyncOpenAI, "_sleep_for_retry", AsyncMock())


@asynccontextmanager
async def _client(transport: httpx2.MockTransport) -> AsyncIterator[openai.AsyncOpenAI]:
    async with get_async_openai(api_key="test-key") as base_client:
        client = base_client.with_options(http_client=httpx2.AsyncClient(transport=transport))
        async with client:
            yield client


async def test_chat_rate_limit_uses_three_total_attempts() -> None:
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(
            429, json={"error": {"message": "rate limited", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}
        )

    async with _client(httpx2.MockTransport(respond)) as client:
        assert client.max_retries == 2
        with pytest.raises(openai.RateLimitError):
            await create_chat_completion(client, {"model": "test", "messages": []})

    assert attempts == 3


async def test_chat_server_error_uses_three_total_attempts() -> None:
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(503, json={"error": {"message": "unavailable", "type": "server_error"}})

    async with _client(httpx2.MockTransport(respond)) as client:
        with pytest.raises(openai.InternalServerError):
            await create_chat_completion(client, {"model": "test", "messages": []})

    assert attempts == 3


async def test_chat_connection_error_uses_three_total_attempts() -> None:
    attempts = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ConnectError("connection failed", request=request)

    async with _client(httpx2.MockTransport(respond)) as client:
        with pytest.raises(openai.APIConnectionError):
            await create_chat_completion(client, {"model": "test", "messages": []})

    assert attempts == 3


async def test_context_length_error_translates_without_retry() -> None:
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(
            400,
            content=json.dumps(
                {
                    "error": {
                        "message": "context too long",
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                    }
                }
            ),
            headers={"content-type": "application/json"},
        )

    async with _client(httpx2.MockTransport(respond)) as client:
        model = BoundOpenAIModel(client=client, model="test")
        with pytest.raises(ContextLengthExceededError):
            await model.responses_create(ResponsesRequest(input="too long"))

    assert attempts == 1


async def test_chat_helper_rejects_streaming_without_request() -> None:
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(200, json={})

    async with _client(httpx2.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="does not support streaming"):
            await create_chat_completion(client, {"model": "test", "messages": [], "stream": True})

    assert attempts == 0


if __name__ == "__main__":
    pytest_bazel.main()
