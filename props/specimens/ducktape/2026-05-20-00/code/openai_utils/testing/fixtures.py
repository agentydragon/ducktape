"""Shared pytest fixtures and utilities for OpenAI testing."""

from __future__ import annotations

import enum
import json
import os

import httpx
import openai
import pytest

from openai_utils.client_factory import build_client
from openai_utils.model import BoundOpenAIModel, OpenAIModelProto


class ClientMode(enum.Enum):
    MOCK = "mock"
    LIVE = "live"


mock_and_live = pytest.mark.parametrize(
    "mode",
    [
        pytest.param(ClientMode.MOCK, id="mock"),
        pytest.param(ClientMode.LIVE, id="live", marks=pytest.mark.live_openai_api),
    ],
)


def error_transport(code: str, message: str) -> httpx.MockTransport:
    """Transport returning a 400 with an OpenAI-shaped error body."""
    body = json.dumps({"error": {"message": message, "type": "invalid_request_error", "code": code}})
    return httpx.MockTransport(
        lambda _request: httpx.Response(400, content=body, headers={"content-type": "application/json"})
    )


def mock_openai_client(transport: httpx.MockTransport) -> OpenAIModelProto:
    """Build BoundOpenAIModel backed by a mock transport (no retries)."""
    inner = openai.AsyncOpenAI(api_key="test-key", http_client=httpx.AsyncClient(transport=transport))
    return BoundOpenAIModel(client=inner, model="test")


@pytest.fixture
def live_openai(request: pytest.FixtureRequest) -> openai.AsyncOpenAI:
    """Provide a live AsyncOpenAI client for tests marked with `live_openai_api`.

    For non-marked tests that include this fixture in the signature (e.g.,
    parameterized tests with a mock branch), return a lightweight no-op
    placeholder to avoid constructing a real client.
    """
    if request.node.get_closest_marker("live_openai_api") is not None:
        return openai.AsyncOpenAI()

    class _Noop:
        pass

    return _Noop()  # type: ignore[return-value]


@pytest.fixture
def live_openai_model() -> str:
    """Return the model to use for live tests."""
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


@pytest.fixture(
    params=[
        pytest.param(ClientMode.MOCK, id="mock"),
        pytest.param(ClientMode.LIVE, id="live", marks=pytest.mark.live_openai_api),
    ]
)
def mock_or_live(request, live_openai_model):
    """Parametrized fixture that replaces @mock_and_live + if/else branching.

    In MOCK mode, delegates to mock_cls.mock() so the generator runs.
    In LIVE mode, returns a live OpenAI client; the generator is never executed.

    Usage::

        async def test_foo(mock_or_live, ...):
            @mock_or_live(MCPDecoratorMock)
            def client(m: MCPDecoratorMock):
                yield
                yield m.assistant_text("done")

            agent = await Agent.create(client=client, ...)
    """
    mode = request.param

    def factory(mock_cls, *args, check_consumed=True, **kwargs):
        if mode is ClientMode.LIVE:
            live_client = build_client(live_openai_model)
            return lambda fn: live_client
        return mock_cls.mock(*args, check_consumed=check_consumed, **kwargs)

    return factory
