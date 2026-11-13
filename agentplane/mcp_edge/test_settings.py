"""The Python deployment contract accepts optional MCP Identity cutover settings."""

import pytest_bazel

from agentplane.mcp_edge.settings import Settings


def _settings_source() -> dict[str, object]:
    return {
        "database_url": "postgresql://edge:edge@postgres.invalid/edge",
        "actions_target": "actions:8080",
        "policy_namespace": "test-policy",
        "caller_service_account_namespaces": ["test-callers"],
        "operator_oidc": {
            "issuer": "https://operator.example",
            "audience": "test-edge",
            "jwks_uri": "https://operator.example/jwks",
        },
    }


def test_identity_cutover_settings_are_accepted() -> None:
    source = _settings_source()
    settings = Settings.model_validate(source)
    assert settings.policy_namespace == "test-policy"
    assert settings.actions_token_file is None

    source["actions_token_file"] = "/var/run/actions"
    settings = Settings.model_validate(source)
    assert settings.policy_namespace == "test-policy"
    assert settings.actions_token_file == "/var/run/actions"


if __name__ == "__main__":
    pytest_bazel.main()
