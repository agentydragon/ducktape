"""The Python deployment contract accepts optional Connection delegation settings."""

import pytest_bazel

from agentplane.action_service.settings import Settings


def _settings_source() -> dict[str, object]:
    return {
        "database_url": "postgresql://actions:actions@postgres.invalid/actions",
        "log_levels": {},
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "auth": [{"mode": "kubernetes_bearer", "audiences": ["test-actions"]}],
        },
        "policy_namespace": "test-agentplane",
        "caller_service_account_namespaces": ["test-agentplane"],
    }


def test_connection_delegation_actor_is_optional_and_accepted() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.connection_delegation_actor is None

    source["connection_delegation_actor"] = {"namespace": "test-agentplane", "name": "mcp-edge"}
    settings = Settings.model_validate(source)
    assert settings.connection_delegation_actor is not None
    assert settings.connection_delegation_actor.namespace == "test-agentplane"
    assert settings.connection_delegation_actor.name == "mcp-edge"


if __name__ == "__main__":
    pytest_bazel.main()
