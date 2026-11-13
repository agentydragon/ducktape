"""The Python deployment contract holds MCP Identity policy and listener settings."""

import pytest_bazel

from agentplane.mcp_edge.settings import Settings


def _settings_source() -> dict[str, object]:
    return {
        "actions_target": "actions:8080",
        "policy_namespace": "test-policy",
        "operator_oidc": {
            "issuer": "https://operator.example",
            "audience": "test-edge",
            "jwks_uri": "https://operator.example/jwks",
        },
    }


def test_identity_settings_are_typed() -> None:
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
