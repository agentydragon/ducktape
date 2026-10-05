"""`ConsoleConfigFile` cross-reference validation: policy cycles, and the access profiles, Recall indexes,
Kubernetes authorization and in-process MCP servers that the config must declare."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.mcp_config import ConsoleConfigFile

_MANUAL_AUTHORITY_CONFIG = {
    "auto_approval_policies": [{"id": "manual", "type": "never"}],
    "access_profiles": [{"id": "manual", "auto_approval_policy": "manual"}],
    "default_access_profile_id": "manual",
}


def test_recall_profile_grants_require_declared_indexes() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            **_MANUAL_AUTHORITY_CONFIG,
            "recall_indexes": {
                "ducktape_public": {"index_id": "ducktape-public", "index_type": "git", "repo_url": "https://example"}
            },
            "access_profiles": [
                {"id": "manual", "auto_approval_policy": "manual", "recall_index_ids": ["ducktape-public"]}
            ],
        }
    )
    assert config.access_profiles[0].recall_index_ids == {"ducktape-public"}

    with pytest.raises(ValueError, match="unknown Recall indexes"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "recall_indexes": {
                    "ducktape_public": {
                        "index_id": "ducktape-public",
                        "index_type": "git",
                        "repo_url": "https://example",
                    }
                },
                "access_profiles": [
                    {"id": "manual", "auto_approval_policy": "manual", "recall_index_ids": ["haku-state"]}
                ],
            }
        )


def test_profile_in_process_server_grants_require_configured_in_process_servers() -> None:
    configured = {
        **_MANUAL_AUTHORITY_CONFIG,
        "mcp": {
            "servers": {
                "haku_index": {"id": "haku_index", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
            }
        },
        "access_profiles": [
            {"id": "manual", "auto_approval_policy": "manual", "in_process_server_ids": ["haku_index"]}
        ],
    }
    config = ConsoleConfigFile.model_validate(configured)
    assert config.access_profiles[0].in_process_server_ids == {"haku_index"}

    with pytest.raises(ValueError, match="unknown in-process MCP servers"):
        ConsoleConfigFile.model_validate(
            {
                **configured,
                "access_profiles": [
                    {"id": "manual", "auto_approval_policy": "manual", "in_process_server_ids": ["missing"]}
                ],
            }
        )


def test_policy_config_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="contains a cycle"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "auto_approval_policies": [
                    {"id": "one", "type": "any_of", "policies": ["two"]},
                    {"id": "two", "type": "any_of", "policies": ["one"]},
                    {"id": "manual", "type": "never"},
                ],
            }
        )


def test_profile_config_rejects_unknown_static_agent_profile() -> None:
    with pytest.raises(ValidationError, match="unknown access profile"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "static_agents": {
                    "test": {
                        "agent_id": "00000000-0000-0000-0000-000000000002",
                        "display_name": "Test Agent",
                        "token": "test-agent-token",
                        "operator_subject": "test-agent-operator",
                        "access_profile_id": "missing",
                    }
                },
            }
        )


def test_profile_config_rejects_unknown_kubernetes_authorization_profile() -> None:
    with pytest.raises(ValidationError, match="Kubernetes authorization references unknown access profiles"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "kubernetes_authorization": {
                    "subjects_by_access_profile": {"missing": {"username": "system:serviceaccount:ns:reader"}}
                },
            }
        )


def test_kubernetes_server_requires_authorization_configuration() -> None:
    with pytest.raises(ValidationError, match="requires Kubernetes authorization configuration"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "mcp": {
                    "servers": {
                        "kubernetes": {
                            "id": "kubernetes",
                            "backend": {"kind": "in_process", "credential": {"kind": "none"}},
                        }
                    }
                },
            }
        )


def test_default_access_profile_does_not_require_a_never_policy() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            "auto_approval_policies": [
                {"id": "operator_review", "type": "never"},
                {"id": "selected_by_default", "type": "any_of", "policies": ["operator_review"]},
            ],
            "access_profiles": [{"id": "operator-default", "auto_approval_policy": "selected_by_default"}],
            "default_access_profile_id": "operator-default",
        }
    )

    assert config.default_access_profile_id == "operator-default"


if __name__ == "__main__":
    pytest_bazel.main()
