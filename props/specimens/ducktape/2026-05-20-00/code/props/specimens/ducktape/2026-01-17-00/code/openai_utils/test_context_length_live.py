from __future__ import annotations

import pytest

from openai_utils.client_factory import build_client
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import ResponsesRequest
from openai_utils.retry import chat_create_with_retries


def _huge_prompt(length: int = 700_000) -> str:
    """Return a deliberately oversized prompt to trigger context-length errors."""

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
