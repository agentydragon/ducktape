"""Unit tests for the Haku agent assembly — pure logic, no network or LLM."""

from pathlib import Path

import pytest_bazel
from agent_framework import InMemoryHistoryProvider

from haku.runtime.agent.agent import _run_command, aclose_history, build_history_provider, build_mcp_tools
from haku.runtime.agent.config import Settings


def _settings(*, console_token: str | None = None, redis_url: str | None = None) -> Settings:
    return Settings(
        model="prov/model",
        litellm_base_url="http://litellm/v1",
        litellm_api_key="k",
        console_token=console_token,
        redis_url=redis_url,
    )


def test_build_mcp_tools_omits_console_without_token() -> None:
    assert build_mcp_tools(_settings()) == []


def test_build_mcp_tools_includes_console_with_token() -> None:
    assert [tool.name for tool in build_mcp_tools(_settings(console_token="secret"))] == ["haku_console"]


def test_history_provider_in_memory_without_redis_url() -> None:
    assert isinstance(build_history_provider(_settings()), InMemoryHistoryProvider)


async def test_history_provider_redis_with_url() -> None:
    history = build_history_provider(_settings(redis_url="redis://localhost:6379"))
    assert not isinstance(history, InMemoryHistoryProvider)
    await aclose_history(history)


async def test_run_command_returns_combined_output(tmp_path: Path) -> None:
    assert (await _run_command("echo hi", cwd=tmp_path)).strip() == "hi"


if __name__ == "__main__":
    pytest_bazel.main()
