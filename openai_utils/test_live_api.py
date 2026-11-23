"""Dual mock/live tests for OpenAI API error translation and smoke tests.

Mock tests use httpx.MockTransport to return canned HTTP responses, letting
the OpenAI SDK parse error bodies the same way it would from real servers.
Live tests hit the real OpenAI API to confirm end-to-end behavior.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from openai_utils.client_factory import build_client
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import Model, ResponsesRequest
from openai_utils.testing.fixtures import ClientMode, error_transport, mock_and_live, mock_openai_client


def _huge_prompt(length: int = 5_000_000) -> str:
    """5M chars is ~1.25M tokens, exceeding even the largest context windows."""
    return "x" * length


@mock_and_live
async def test_context_length_exceeded(mode: ClientMode, live_openai_model) -> None:
    """Context length exceeded → ContextLengthExceededError in both mock and live."""
    if mode is ClientMode.MOCK:
        client: Model = mock_openai_client(error_transport("context_length_exceeded", "context length exceeded"))
    else:
        client = build_client(live_openai_model)

    with pytest.raises(ContextLengthExceededError):
        await client.sample(client.prepare(ResponsesRequest(input=_huge_prompt(), max_output_tokens=16)))


if __name__ == "__main__":
    pytest_bazel.main()
