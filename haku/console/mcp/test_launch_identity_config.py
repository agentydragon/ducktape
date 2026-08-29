"""Deploy-time launch identity and profile graph validation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.harnesses.kind import HarnessKind
from haku.console.mcp_config import ConsoleConfigFile

_AGENT = UUID("00000000-0000-4000-8000-000000000001")


def _runtime() -> dict[str, object]:
    return {
        "agent_id": str(_AGENT),
        "namespace": "sandbox",
        "warm_pool": "pool",
        "claim_prefix": "claude",
        "harness_label": "claude",
        "cwd": "/workspace",
        "session_ttl_seconds": 300,
        "https_proxy": "https://proxy.example",
        "ca_bundle": "/ca.pem",
        "no_proxy": "localhost",
        "mcp_url": "https://console.example/mcp",
        "implementation": {
            "kind": "claude_code",
            "api_base_url": "http://litellm.test:4000",
            "model": "anthropic-max20/ant-messages/claude-sonnet-5",
            "haiku_model": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
            "auth_token_placeholder": "placeholder",
        },
    }


def _config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "auto_approval_policies": [{"id": "manual", "type": "never"}],
        "access_profiles": [
            {
                "id": "chat",
                "auto_approval_policy": "manual",
                "allowed_harnesses": ["claude_code"],
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
        "launchable_agents": [{"agent_id": str(_AGENT), "system_prompt_template": "/prompt"}],
    }
    value.update(overrides)
    return value


def test_launchable_agents_and_runtime_edges_are_deploy_config() -> None:
    config = ConsoleConfigFile.model_validate(_config())
    assert config.launchable_agents[0].agent_id == _AGENT
    assert config.launchable_agents[0].system_prompt_template == Path("/prompt")
    assert config.access_profiles[0].allowed_harnesses == {HarnessKind.CLAUDE_CODE}


def test_allowed_chat_runtimes_is_rejected_after_contract() -> None:
    with pytest.raises(ValidationError, match="allowed_chat_runtimes"):
        ConsoleConfigFile.model_validate(
            _config(
                access_profiles=[
                    {"id": "chat", "auto_approval_policy": "manual", "allowed_chat_runtimes": ["claude_code"]}
                ]
            )
        )


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
            _config(
                launchable_agents=[
                    {"agent_id": "00000000-0000-0000-0000-000000000099", "system_prompt_template": "/prompt"}
                ]
            )
        )


def test_configured_runtime_requires_launchable_agents_and_runtime_enabled_profiles() -> None:
    runtime = _runtime()
    with pytest.raises(ValidationError, match="harness Agents are not launchable"):
        ConsoleConfigFile.model_validate(_config(harnesses={"claude_code": runtime}, launchable_agents=[]))
    with pytest.raises(ValidationError, match="profile disallows claude_code"):
        ConsoleConfigFile.model_validate(
            _config(
                harnesses={"claude_code": runtime}, access_profiles=[{"id": "chat", "auto_approval_policy": "manual"}]
            )
        )


def test_runtime_label_is_rejected_after_contract() -> None:
    registration = dict(_runtime())
    registration["runtime_label"] = registration.pop("harness_label")
    with pytest.raises(ValidationError, match="runtime_label"):
        ConsoleConfigFile.model_validate(_config(harnesses={"claude_code": registration}))


def test_default_chat_agent_id_is_rejected_after_contract() -> None:
    """The loader ignores unknown keys (the shared YAML's `settings` section), so a config still
    spelling the retired `default_chat_agent_id` key must fail loudly rather than silently launching
    without a default Agent."""
    stale = _config(default_chat_agent_id=str(_AGENT))
    with pytest.raises(ValidationError, match="default_chat_agent_id was renamed"):
        ConsoleConfigFile.model_validate(stale)


def test_chat_runtimes_key_is_rejected() -> None:
    """The loader ignores unknown keys (the shared YAML's `settings` section), so a config still
    spelling the retired `chat_runtimes` key must fail loudly rather than silently losing every
    harness."""
    with pytest.raises(ValidationError, match="chat_runtimes was renamed to harnesses"):
        ConsoleConfigFile.model_validate(_config(chat_runtimes={"claude_code": _runtime()}))


def test_launchable_agent_requires_its_own_runtime_registration() -> None:
    second = UUID("00000000-0000-4000-8000-000000000002")
    static_agents = _config()["static_agents"]
    assert isinstance(static_agents, list)
    runtime = _runtime()
    with pytest.raises(ValidationError, match="has no configured harness registration"):
        ConsoleConfigFile.model_validate(
            _config(
                harnesses={"claude_code": runtime},
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
                launchable_agents=[
                    {"agent_id": str(_AGENT), "system_prompt_template": "/prompt"},
                    {"agent_id": str(second), "system_prompt_template": "/second-prompt"},
                ],
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
