"""Coverage for the ANTHROPIC branch of the MAF client builder.

The agent e2e tests only exercise the OpenAI shapes, so the Anthropic wiring went unrun
until a cluster anthropic model existed — and shipped a bug: the SDK posts to
`/v1/messages`, but it was handed `OPENAI_BASE_URL` (`.../v1`), so it hit `/v1/v1/messages`
and 404'd. These tests pin the URL handling and drive a real MAF agent turn built by
`build_chat_client` against a mock `/v1/messages` backend (respx). A regression to the
wrong path would find no registered route and fail.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel
import respx
from agent_framework import Agent

from openai_utils.api_shape import LLMApiShape
from props.agents.af.client import anthropic_base_url, build_chat_client

_PROXY = "http://props-llm-proxy:8000"


def test_anthropic_base_url_strips_v1() -> None:
    # OPENAI_BASE_URL ends in /v1; the anthropic SDK appends /v1/messages itself.
    assert anthropic_base_url(f"{_PROXY}/v1") == _PROXY
    # No-op when already bare (defensive).
    assert anthropic_base_url(_PROXY) == _PROXY


def test_default_options_are_shape_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    # store=False only for the Responses client (stateless); never for Anthropic (the SDK
    # rejects unknown kwargs) and not needed for chat-completions (stateless already).
    monkeypatch.setenv("OPENAI_BASE_URL", f"{_PROXY}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "creds")
    assert build_chat_client("m", LLMApiShape.RESPONSES).default_options == {"store": False}
    assert build_chat_client("m", LLMApiShape.ANTHROPIC).default_options == {}
    assert build_chat_client("m", LLMApiShape.CHAT_COMPLETIONS).default_options == {}


def _anthropic_message_response(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "glm-4.6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


async def test_anthropic_agent_turn_hits_v1_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_chat_client(ANTHROPIC) + a MAF agent turn must reach the proxy's `/v1/messages`
    exactly (not `/v1/v1/messages`). The env mirrors what the orchestrator sets: an
    OPENAI_BASE_URL ending in `/v1`."""
    monkeypatch.setenv("OPENAI_BASE_URL", f"{_PROXY}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "creds")

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{_PROXY}/v1/messages").mock(
            return_value=httpx.Response(200, json=_anthropic_message_response("hello from glm"))
        )
        setup = build_chat_client("glm-4.6", LLMApiShape.ANTHROPIC)
        # The Anthropic SDK rejects a `store` kwarg client-side; it must not be a default option.
        assert "store" not in setup.default_options
        agent = Agent(client=setup.client, instructions="You are a test agent.", default_options=setup.default_options)
        result = await agent.run("hi")

    assert route.called
    # The agent authenticates to the proxy via Authorization: Bearer (auth_token), not x-api-key.
    assert route.calls.last.request.headers["authorization"] == "Bearer creds"
    assert "hello from glm" in result.text


if __name__ == "__main__":
    pytest_bazel.main()
