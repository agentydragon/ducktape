"""Access profiles may only reference Recall indexes and in-process MCP servers the config declares."""

from __future__ import annotations

import pytest
import pytest_bazel

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


if __name__ == "__main__":
    pytest_bazel.main()
