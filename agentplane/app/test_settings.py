"""The settings models consumed by the Deployment and browser-safe frontend config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import AnyHttpUrl, ValidationError

from agentplane.app.oidc import OIDCSettings
from agentplane.app.settings import FrontendConfig, LangfuseSearchSettings, Settings, TraceSearchSettings
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS

APP_ENVIRONMENT = {
    "AGENTPLANE_SANDBOX_NAMESPACE": "test-sandbox-namespace",
    "AGENTPLANE_RUNNER_GATEWAY_TARGET": "runner-gateway.test:8080",
    "AGENTPLANE_SESSION_DISPATCHER_TARGET": "session-dispatcher.test:8080",
    "AGENTPLANE_RUNNER_GATEWAY_CONTROL_TARGET": "{sandbox}-control.{namespace}.test:8080",
    "AGENTPLANE_RUNNER_GATEWAY_TOKEN_FILE": "/tokens/runner-gateway",
    "AGENTPLANE_HISTORY_SERVICE_TARGET": "history-service.test:8080",
    "AGENTPLANE_DATABASE_URL": "postgresql://test@test.invalid/test",
    "AGENTPLANE_MODELS": json.dumps(
        {
            "models": [
                {"model": model, "display_name": display, "reasoning_efforts": list(TEST_REASONING_EFFORTS)}
                for model, display in (
                    ("test-claude-model", "Test Claude Model"),
                    ("test-codex-model", "Test Codex Model"),
                )
            ],
            "harnesses": {"HARNESS_CLAUDE": ["test-claude-model"], "HARNESS_CODEX": ["test-codex-model"]},
        }
    ),
    "AGENTPLANE_EGRESS_ADMIN_URL": "http://egress.test.invalid:8081",
    "AGENTPLANE_CONNECTION_SERVICE_URL": "http://connection-service.test.invalid:8080",
    "AGENTPLANE_NOTIFICATIONS_URL": "http://notifications.test.invalid:8080",
    "AGENTPLANE_NOTIFICATIONS_TOKEN_FILE": "/test/notifications-token",
    "AGENTPLANE_ELECTRIC_URL": "http://electric.test.invalid:3000",
    "AGENTPLANE_ACTION_FEDERATION": json.dumps(
        {
            "mode": "direct",
            "service_url": "http://actions.test.invalid:8080",
            "login_jwks_uri": "https://auth.test.invalid/jwks/",
            "target": {
                "issuer": "https://auth.test.invalid/application/o/test-app/",
                "audience": "test-client",
                "jwks_uri": "https://auth.test.invalid/jwks/",
            },
            "scope": "openid profile",
        }
    ),
}
OIDC_ENVIRONMENT = {
    "AGENTPLANE_OIDC_ISSUER": "https://auth.test.invalid/application/o/test-app/",
    "AGENTPLANE_OIDC_CLIENT_ID": "test-client",
    "AGENTPLANE_OIDC_CLIENT_SECRET": "test-client-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_SESSION_SECRET": "test-session-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_PUBLIC_BASE_URL": "https://app.test.invalid",
}


def set_app_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in APP_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    config = tmp_path / "config.yaml"
    config.write_text("default_policy_sets: [test-basic]\n")
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))


def test_the_two_settings_models_read_environment_and_config_without_colliding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AGENTPLANE_OIDC_` sits inside `AGENTPLANE_`, and the staging Deployment sets both.

    A model that claimed the other's variables, or refused to parse alongside them, would take the
    app down at rollout rather than here.
    """
    set_app_environment(monkeypatch, tmp_path)
    for name, value in OIDC_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_cli_parse_args=[])
    oidc = OIDCSettings()

    assert settings.sandbox_namespace == "test-sandbox-namespace"
    assert settings.runner_gateway_request_timeout_s == 20
    assert settings.runner_gateway_lifecycle_timeout_s == 310
    assert (settings.listen_address, settings.token_audience) == ("127.0.0.1:8080", "agentplane")
    assert settings.session_dispatcher_target == "session-dispatcher.test:8080"
    assert "session_dispatcher_http_url" not in Settings.model_fields
    assert str(settings.session_dispatcher_token_file) == "/var/run/secrets/agentplane-session-dispatcher/token"
    assert oidc.public_base_url == "https://app.test.invalid"


def test_rest_api_settings_do_not_require_electric(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENTPLANE_ELECTRIC_URL")
    settings = Settings(_cli_parse_args=[])
    assert settings.electric_url is None
    assert settings.frontend_config == FrontendConfig()


def test_frontend_config_loads_from_the_deployment_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_policy_sets: [test-basic]\n"
        "frontend_config:\n"
        "  trace_search:\n"
        "    grafana_base_url: https://grafana.test.invalid\n"
        "    tempo_datasource_uid: tempo\n"
        "    deployment_environment: agentplane-staging\n"
        "  langfuse_search:\n"
        "    base_url: https://langfuse.test.invalid\n"
        "    project_id: test-project\n"
        "    sandbox_namespace_metadata_key: attributes.agentplane.namespace\n"
        "    sandbox_service_account_metadata_key: attributes.agentplane.service_account\n"
    )
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))

    assert Settings(_cli_parse_args=[]).frontend_config == FrontendConfig(
        trace_search=TraceSearchSettings(
            grafana_base_url=AnyHttpUrl("https://grafana.test.invalid"),
            tempo_datasource_uid="tempo",
            deployment_environment="agentplane-staging",
        ),
        langfuse_search=LangfuseSearchSettings(
            base_url=AnyHttpUrl("https://langfuse.test.invalid"),
            project_id="test-project",
            sandbox_namespace_metadata_key="attributes.agentplane.namespace",
            sandbox_service_account_metadata_key="attributes.agentplane.service_account",
        ),
    )


@pytest.mark.parametrize("default_policy_sets", [None, [], ["test-basic"]])
def test_default_policy_sets_are_required_for_direct_lifecycle(
    default_policy_sets: list[str] | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in APP_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENTPLANE_DEFAULT_POLICY_SETS", raising=False)
    # This value is deployed through the YAML ConfigMap, not an environment string.
    config = tmp_path / "config.yaml"
    config.write_text(
        "" if default_policy_sets is None else f"default_policy_sets: {json.dumps(default_policy_sets)}\n"
    )
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))

    if default_policy_sets is None:
        with pytest.raises(ValidationError, match="default_policy_sets"):
            Settings(_cli_parse_args=[])
    else:
        settings = Settings(_cli_parse_args=[])
        assert settings.default_policy_sets == set(default_policy_sets)


@pytest.mark.parametrize(
    "field",
    [
        "AGENTPLANE_EGRESS_ADMIN_URL",
        "AGENTPLANE_CONNECTION_SERVICE_URL",
        "AGENTPLANE_NOTIFICATIONS_URL",
        "AGENTPLANE_ELECTRIC_URL",
    ],
)
def test_endpoint_settings_reject_query_components(field: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(field, "http://service.test.invalid/?tenant=one")

    with pytest.raises(ValidationError):
        Settings(_cli_parse_args=[])


if __name__ == "__main__":
    pytest_bazel.main()
