"""`ConsoleConfigFile` cross-reference validation for Agent profiles and in-process servers."""

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
_NO_CREDENTIAL_BACKEND = {"kind": "in_process", "credential": {"kind": "none"}}


def test_profile_in_process_server_grants_require_configured_in_process_servers() -> None:
    configured = {
        **_MANUAL_AUTHORITY_CONFIG,
        "mcp": {
            "servers": {"grants": {"id": "grants", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}}
        },
        "access_profiles": [{"id": "manual", "auto_approval_policy": "manual", "in_process_server_ids": ["grants"]}],
    }
    config = ConsoleConfigFile.model_validate(configured)
    assert config.access_profiles[0].in_process_server_ids == {"grants"}

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
    with pytest.raises(ValidationError):
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
    with pytest.raises(ValidationError):
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
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "kubernetes_authorization": {
                    "subjects_by_access_profile": {"missing": {"username": "system:serviceaccount:ns:reader"}}
                },
            }
        )


def test_duplicate_mcp_server_ids_fail_config_validation() -> None:
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "mcp": {
                    "servers": {
                        "grocy_one": {"id": "grocy", "backend": _NO_CREDENTIAL_BACKEND},
                        "grocy_two": {"id": "grocy", "backend": _NO_CREDENTIAL_BACKEND},
                    }
                },
            }
        )


def test_duplicate_sanitized_mcp_server_prefixes_fail_config_validation() -> None:
    with pytest.raises(ValidationError):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "mcp": {
                    "servers": {
                        "grocy_hyphen": {"id": "grocy-sf", "backend": _NO_CREDENTIAL_BACKEND},
                        "grocy_underscore": {"id": "grocy_sf", "backend": _NO_CREDENTIAL_BACKEND},
                    }
                },
            }
        )


def test_duplicate_static_agent_ids_fail_config_validation() -> None:
    agent = {
        "agent_id": "00000000-0000-0000-0000-000000000003",
        "display_name": "Test Agent",
        "token": "test-agent-token",
        "operator_subject": "test-agent-operator",
        "access_profile_id": "manual",
    }
    with pytest.raises(ValidationError, match="duplicate static Agent id"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "static_agents": {"first": agent, "second": {**agent, "display_name": "Other Agent"}},
            }
        )


def test_kubernetes_server_requires_authorization_configuration() -> None:
    with pytest.raises(ValidationError):
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


if __name__ == "__main__":
    pytest_bazel.main()
