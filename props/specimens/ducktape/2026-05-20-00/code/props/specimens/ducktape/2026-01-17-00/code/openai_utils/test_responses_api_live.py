"""Live tests for OpenAI Responses API (streaming and non-streaming)."""

import os
from typing import Any, cast

import openai
import pytest
from openai.types.responses import EasyInputMessageParam, ResponseInputParam


@pytest.mark.live_openai_api
async def test_responses_nonstreaming_live(tmp_path):
    """Live test: call OpenAI Responses.create (non-streaming).

    Requires OPENAI_API_KEY in the environment. Uses OPENAI_MODEL if set or
    falls back to 'o4-mini'.
    """

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    # Use TypedDict (input type) directly
    inp: list[EasyInputMessageParam] = [
        {"type": "message", "role": "user", "content": "Say hello in one short sentence."}
    ]

    # Non-streaming call
    resp = await client.responses.create(model=model, input=cast(ResponseInputParam, inp))

    # OpenAI SDK returns Pydantic models with model_dump
    data = resp.model_dump(exclude_none=True)
    # Expect an 'id' or 'object' token from Responses API
    assert ("id" in data) or (data.get("object") is not None)


@pytest.mark.live_openai_api
async def test_responses_streaming_live(tmp_path):
    """Live test: call OpenAI Responses.create with stream=True and iterate.

    The test collects streamed events and asserts that at least one event was received.
    """

    client = openai.AsyncOpenAI()
    model = os.getenv("OPENAI_MODEL", "o4-mini")

    # Use TypedDict (input type) directly
    inp: list[EasyInputMessageParam] = [
        {"type": "message", "role": "user", "content": "Stream: say numbers 1..3 as separate events"}
    ]

    # AsyncOpenAI with stream=True returns an async iterator
    stream = await client.responses.create(model=model, input=cast(ResponseInputParam, inp), stream=True)

    items: list[dict[str, Any]] = []
    async for event in stream:
        items.append(event.model_dump(exclude_none=True))

    assert items, "No stream events received"
