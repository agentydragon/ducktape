"""Tests for RoutineLauncher — the launch_routine fire — over a respx-mocked Anthropic fire URL.

respx patches httpx's real transport, so the async fire call is intercepted without a network hop.
"""

import json

import httpx
import pytest
import pytest_bazel
import respx
from pydantic import SecretStr

from haku.console.config import LaunchRoutineConfig
from haku.console.tools.routine import RoutineLauncher

ROUTINE_ID = "trig_test"
FIRE_URL = f"https://api.anthropic.com/v1/claude_code/routines/{ROUTINE_ID}/fire"


def _launcher() -> RoutineLauncher:
    return RoutineLauncher(LaunchRoutineConfig(routine_id=ROUTINE_ID, token=SecretStr("sk-test-token")))


async def test_launch_fires_with_server_side_bearer_and_no_text() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(
            return_value=httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_x"})
        )
        result = await _launcher().launch(None)
    assert result.session_url == "https://claude.ai/code/session_x"
    sent = route.calls.last.request
    # The bearer + required anthropic-version header are attached server-side.
    assert sent.headers["authorization"] == "Bearer sk-test-token"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(sent.content) == {}


async def test_launch_forwards_custom_text() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200, json={"claude_code_session_url": "u"}))
        await _launcher().launch("scan CPAP and summarize anomalies")
    assert json.loads(route.calls.last.request.content) == {"text": "scan CPAP and summarize anomalies"}


async def test_launch_blank_text_uses_routine_default() -> None:
    with respx.mock:
        route = respx.post(FIRE_URL).mock(return_value=httpx.Response(200, json={"claude_code_session_url": "u"}))
        await _launcher().launch("   ")
    # Blank/whitespace collapses to the routine's saved default (no text field sent).
    assert json.loads(route.calls.last.request.content) == {}


async def test_launch_raises_with_upstream_detail_on_error() -> None:
    with respx.mock:
        respx.post(FIRE_URL).mock(
            return_value=httpx.Response(400, json={"error": {"message": "anthropic-version: header is required"}})
        )
        with pytest.raises(RuntimeError, match="anthropic-version: header is required"):
            await _launcher().launch(None)


if __name__ == "__main__":
    pytest_bazel.main()
