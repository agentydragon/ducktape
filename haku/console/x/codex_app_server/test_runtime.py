"""Codex as the Console's launch adapter -- the runner owns its protocol and projection (#4667)."""

from __future__ import annotations

from typing import Any

import pytest_bazel

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.codex_app_server.config import ReasoningEffort
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter
from haku.console.x.runtime import RuntimeLaunch, RuntimeMcpServer
from haku.runner.codex.options import CodexModelProvider


def _launch(**overrides: Any) -> RuntimeLaunch:
    values: dict[str, Any] = {
        "cwd": "/workspace",
        "environment": {"CODEX_HOME": "/codex-home"},
        "mcp_servers": {
            "haku-console": RuntimeMcpServer(
                url="https://console.test/mcp", bearer_environment_variable="HAKU_SESSION_TOKEN"
            )
        },
        "appended_system_prompt": "you are Haku",
        "resume_from": 29,
    }
    values.update(overrides)
    return RuntimeLaunch(**values)


def test_codex_launch_carries_the_provider_argv_and_the_runner_thread_params() -> None:
    adapter = CodexRuntimeAdapter(
        model="codex-gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.LOW,
        model_provider=CodexModelProvider(
            provider_id="haku",
            name="Haku OpenAI-compatible",
            base_url="http://litellm.test/v1",
            api_key_env_var="OPENAI_API_KEY",
        ),
    )
    launch = adapter.build_launch(_launch())

    assert launch.arguments == (
        "-c",
        'model_provider = "haku"',
        "-c",
        'model_providers = {haku = {name = "Haku OpenAI-compatible", '
        'base_url = "http://litellm.test/v1", env_key = "OPENAI_API_KEY", '
        'wire_api = "responses"}}',
        "-c",
        'mcp_servers = {haku-console = {url = "https://console.test/mcp", bearer_token_env_var = "HAKU_SESSION_TOKEN"}}',
        "app-server",
        "--listen",
        "stdio://",
    )
    assert launch.cwd == "/workspace"
    assert launch.resume_from == 29
    # The runner owns thread/start now, so model, reasoning effort and developer instructions ride
    # the launch environment for the runner's CodexHarness to read.
    assert launch.environment == {
        "CODEX_HOME": "/codex-home",
        "HAKU_CODEX_MODEL": "codex-gpt-5.6-sol",
        "HAKU_CODEX_REASONING_EFFORT": "low",
        "HAKU_CODEX_DEVELOPER_INSTRUCTIONS": "you are Haku",
    }


def test_adapter_identity_is_codex_without_making_it_a_configured_runtime() -> None:
    adapter = CodexRuntimeAdapter()
    assert adapter.kind is HarnessKind.CODEX_APP_SERVER
    assert adapter.display_name == "Codex"


if __name__ == "__main__":
    pytest_bazel.main()
