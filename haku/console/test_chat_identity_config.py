"""Deploy-time chat launch identity and profile graph validation."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.chat_models import RuntimeKind
from haku.console.mcp_config import ConsoleConfigFile

_AGENT = UUID("00000000-0000-4000-8000-000000000001")


def _runtime() -> dict[str, object]:
    return {
        "agent_id": str(_AGENT),
        "namespace": "sandbox",
        "warm_pool": "pool",
        "claim_prefix": "claude",
        "runtime_label": "claude-chat",
        "cwd": "/workspace",
        "session_ttl_seconds": 300,
        "https_proxy": "https://proxy.example",
        "ca_bundle": "/ca.pem",
        "no_proxy": "localhost",
        "mcp_url": "https://console.example/mcp",
        "system_prompt_template": "/prompt",
        "implementation": {"kind": "claude_code", "oauth_placeholder": "placeholder"},
    }


def _config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [
            {
                "id": "chat",
                "auto_approval_policy": "manual",
                "allowed_chat_runtimes": ["claude_code"],
                "can_read_profiles": ["review"],
            },
            {"id": "review", "auto_approval_policy": "manual"},
        ],
        "default_access_profile_id": "chat",
        "static_agents": [
            {
                "agent_id": str(_AGENT),
                "display_name": "Console Agent",
                "token_env_var": "TOKEN",
                "operator_subject_env": "OPERATOR",
                "access_profile_id": "chat",
            }
        ],
        "launchable_agents": [{"agent_id": str(_AGENT)}],
        "default_chat_agent_id": str(_AGENT),
    }
    value.update(overrides)
    return value


def test_launchable_agents_and_runtime_edges_are_deploy_config() -> None:
    config = ConsoleConfigFile.model_validate(_config())
    assert config.launchable_agents[0].agent_id == _AGENT
    assert config.access_profiles[0].allowed_chat_runtimes == {RuntimeKind.CLAUDE_CODE}


def test_profile_read_graph_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="read graph contains a cycle"):
        ConsoleConfigFile.model_validate(
            _config(
                access_profiles=[
                    {"id": "chat", "auto_approval_policy": "manual", "can_read_profiles": ["review"]},
                    {"id": "review", "auto_approval_policy": "manual", "can_read_profiles": ["chat"]},
                ]
            )
        )


def test_launchable_agent_must_be_a_configured_static_agent() -> None:
    with pytest.raises(ValidationError, match="not configured static Agents"):
        ConsoleConfigFile.model_validate(
            _config(launchable_agents=[{"agent_id": "00000000-0000-0000-0000-000000000099"}])
        )


def test_configured_runtime_requires_a_launchable_default_and_runtime_enabled_profiles() -> None:
    runtime = _runtime()
    with pytest.raises(ValidationError, match="default chat Agent must be launchable"):
        ConsoleConfigFile.model_validate(_config(chat_runtimes={"claude_code": runtime}, launchable_agents=[]))
    with pytest.raises(ValidationError, match="profile disallows claude_code"):
        ConsoleConfigFile.model_validate(
            _config(
                chat_runtimes={"claude_code": runtime},
                access_profiles=[{"id": "chat", "auto_approval_policy": "manual"}],
            )
        )


def test_launchable_agent_requires_its_own_runtime_registration() -> None:
    second = UUID("00000000-0000-4000-8000-000000000002")
    static_agents = _config()["static_agents"]
    assert isinstance(static_agents, list)
    runtime = _runtime()
    with pytest.raises(ValidationError, match="has no configured chat runtime registration"):
        ConsoleConfigFile.model_validate(
            _config(
                chat_runtimes={"claude_code": runtime},
                static_agents=[
                    *static_agents,
                    {
                        "agent_id": str(second),
                        "display_name": "Second Console Agent",
                        "token_env_var": "SECOND_TOKEN",
                        "operator_subject_env": "OPERATOR",
                        "access_profile_id": "chat",
                    },
                ],
                launchable_agents=[{"agent_id": str(_AGENT)}, {"agent_id": str(second)}],
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
