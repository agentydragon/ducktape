"""Live tests for OpenAI Responses API and Chat Completions API."""

from __future__ import annotations

import os
from typing import Any, cast

import openai
import pytest
import pytest_bazel
from openai.types.responses import EasyInputMessageParam, ResponseInputParam

from openai_utils.client_factory import build_client
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import ResponsesRequest
from openai_utils.retry import chat_create_with_retries


@pytest.mark.live_openai_api
async def test_responses_nonstreaming_live(tmp_path):
    """Call OpenAI Responses.create (non-streaming) and verify a response is returned."""

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    inp: list[EasyInputMessageParam] = [
        {"type": "message", "role": "user", "content": "Say hello in one short sentence."}
    ]

    resp = await client.responses.create(model=model, input=cast(ResponseInputParam, inp))

    data = resp.model_dump(exclude_none=True)
    assert ("id" in data) or (data.get("object") is not None)


@pytest.mark.live_openai_api
async def test_responses_streaming_live(tmp_path):
    """Call OpenAI Responses.create with stream=True and collect events."""

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    inp: list[EasyInputMessageParam] = [
        {"type": "message", "role": "user", "content": "Stream: say numbers 1..3 as separate events"}
    ]

    stream = await client.responses.create(model=model, input=cast(ResponseInputParam, inp), stream=True)

    items: list[dict[str, Any]] = []
    async for event in stream:
        items.append(event.model_dump(exclude_none=True))

    assert items, "No stream events received"


def _huge_prompt(length: int = 5_000_000) -> str:
    """Return a deliberately oversized prompt to trigger context-length errors.

    5M chars is ~1.25M tokens, exceeding even the largest context windows.
    """
    return "x" * length


@pytest.mark.live_openai_api
async def test_responses_context_length_exceeded_live(require_openai_api_key, live_openai_model) -> None:
    """Responses API: oversized prompt should raise our adapter exception."""

    client = build_client(live_openai_model)

    req = ResponsesRequest(input=_huge_prompt(), max_output_tokens=16)

    with pytest.raises(ContextLengthExceededError):
        await client.responses_create(req)


@pytest.mark.live_openai_api
async def test_chat_context_length_exceeded_live(require_openai_api_key, live_openai_model, live_async_openai) -> None:
    """Chat Completions API: oversized prompt should raise our adapter exception."""

    params = {"model": live_openai_model, "messages": [{"role": "user", "content": _huge_prompt()}], "max_tokens": 8}

    with pytest.raises(ContextLengthExceededError):
        await chat_create_with_retries(live_async_openai, params)


if __name__ == "__main__":
    pytest_bazel.main()
