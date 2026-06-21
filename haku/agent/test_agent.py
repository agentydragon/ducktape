"""Unit tests for the Haku agent assembly — pure logic, no network or LLM."""

from pathlib import Path

import pytest_bazel

from haku.agent.agent import _run_command, build_mcp_tools
from haku.agent.config import Settings


def _settings(*, tana_ro_token: str | None = None) -> Settings:
    return Settings(
        model="prov/model", litellm_base_url="http://litellm/v1", litellm_api_key="k", tana_ro_token=tana_ro_token
    )


def test_build_mcp_tools_omits_tana_without_token() -> None:
    assert build_mcp_tools(_settings()) == []


def test_build_mcp_tools_includes_tana_with_token() -> None:
    assert [tool.name for tool in build_mcp_tools(_settings(tana_ro_token="secret"))] == ["tana_ro"]


async def test_run_command_returns_combined_output(tmp_path: Path) -> None:
    assert (await _run_command("echo hi", cwd=tmp_path)).strip() == "hi"


if __name__ == "__main__":
    pytest_bazel.main()
