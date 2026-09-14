"""Regression tests for the typed scripted model endpoint."""

from __future__ import annotations

import aiohttp
import pytest
import pytest_bazel

from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.harness_tests.model_endpoint import ModelRequestParseError


async def test_invalid_native_request_fails_the_waiting_script_without_exposing_raw_http() -> None:
    async with AnthropicMessages() as messages, aiohttp.ClientSession() as client:
        response = await client.post(messages.origin, json={"not": "an Anthropic Messages request"})
        assert response.status == 400

        with pytest.raises(ModelRequestParseError, match="typed model fixture rejected a native request"):
            await messages.await_next_request()


if __name__ == "__main__":
    pytest_bazel.main()
