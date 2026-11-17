"""Typed static deployment settings; runtime Secrets and environment loading belong to the Go process."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentplane.net.peertls.config import (
    ACTIONS_URI,
    HISTORY_SERVICE_URI,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from agentplane.notification_service.settings import (
    GitHubSettings,
    NoticeDebounceSettings,
    QuotaSettings,
    RunnerGatewaySettings,
    SessionDispatcherSettings,
    Settings,
)


def static_values() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "auth": [{"mode": "spiffe", "ca_file": "/test/ca.pem"}],
            "tls": {"cert_file": "/test/tls.crt", "key_file": "/test/tls.key"},
        },
        "namespace": "test",
        "actions": {"url": "http://actions", "token_file": "/tokens/actions"},
        "runner_gateway": {"control_target": "runner-gateway.{namespace}.test:8080", "token_file": "/tokens/gateway"},
        "history_service": {"target": "history:8080", "token_file": "/tokens/history"},
        "session_dispatcher": {"target": "dispatcher:8080", "token_file": "/tokens/dispatcher"},
    }


def test_settings_are_typed_and_include_nested_defaults() -> None:
    values = static_values()
    values["quotas"] = {"inboxes_per_account": 12, "active_subscriptions_per_inbox": 24, "entries_per_inbox": 500}
    values["notice_debounce"] = {"quiet_seconds": 3, "max_wait_seconds": 15}
    settings = Settings.model_validate(values)
    assert settings.quotas == QuotaSettings(
        inboxes_per_account=12, active_subscriptions_per_inbox=24, entries_per_inbox=500
    )
    assert settings.notice_debounce == NoticeDebounceSettings(quiet_seconds=3, max_wait_seconds=15)
    assert str(settings.actions.url) == "http://actions/"
    assert settings.actions.token_file == Path("/tokens/actions")
    assert settings.session_dispatcher == SessionDispatcherSettings(
        target="dispatcher:8080", token_file=Path("/tokens/dispatcher"), request_timeout_s=15
    )
    assert settings.unavailable_destination_retry_s == 30
    assert settings.request_timeout_s == 15
    assert settings.message_body_limit_bytes == 32 * 1024
    assert settings.github_delivery_retention_days == 7
    assert "operator_reader_services" not in settings.model_dump()


def test_outbound_service_svid_authentication_needs_no_token_file() -> None:
    values = static_values()

    def client_tls(server_uri: str) -> dict[str, str]:
        return {
            "ca_file": "/trust/agentplane.pem",
            "server_uri": server_uri,
            "cert_file": "/tls/notifications.crt",
            "key_file": "/tls/notifications.key",
            "client_uri": NOTIFICATIONS_URI,
        }

    values["actions"] = {"url": "http://actions", "tls": client_tls(ACTIONS_URI)}
    values["runner_gateway"] = {
        "control_target": "runner-gateway.{namespace}.test:8080",
        "tls": client_tls(SESSION_DISPATCHER_URI),
    }
    values["history_service"] = {"target": "history:8080", "tls": client_tls(HISTORY_SERVICE_URI)}
    values["session_dispatcher"] = {"target": "dispatcher:8080", "tls": client_tls(SESSION_DISPATCHER_URI)}

    settings = Settings.model_validate(values)

    assert settings.actions.token_file is None
    assert settings.runner_gateway.token_file is None
    assert settings.history_service.token_file is None
    assert settings.session_dispatcher.token_file is None


@pytest.mark.parametrize("server_uri", [SESSION_DISPATCHER_URI, SANDBOX_CONTROLLER_URI])
def test_runtime_proxy_peer_accepts_each_cutover_receiver(server_uri: str) -> None:
    tls = {
        "ca_file": "/test/ca.pem",
        "server_uri": server_uri,
        "cert_file": "/test/tls.crt",
        "key_file": "/test/tls.key",
        "client_uri": NOTIFICATIONS_URI,
    }
    configured = RunnerGatewaySettings.model_validate({"control_target": "proxy.test:8082", "tls": tls})
    assert configured.token_file is None
    assert configured.tls is not None
    assert configured.tls.server_uri == server_uri
    with pytest.raises(ValidationError):
        SessionDispatcherSettings.model_validate(
            {"target": "dispatcher.test:8080", "tls": {**tls, "server_uri": SANDBOX_CONTROLLER_URI}}
        )
    with pytest.raises(ValidationError):
        RunnerGatewaySettings.model_validate(
            {"control_target": "proxy.test:8082", "tls": {**tls, "server_uri": ACTIONS_URI}}
        )


def test_outbound_service_svid_authentication_rejects_mixed_or_wrong_callers() -> None:
    action_settings = {
        "url": "http://actions",
        "tls": {
            "ca_file": "/trust/agentplane.pem",
            "server_uri": ACTIONS_URI,
            "cert_file": "/tls/notifications.crt",
            "key_file": "/tls/notifications.key",
            "client_uri": NOTIFICATIONS_URI,
        },
        "token_file": "/tokens/actions",
    }
    values = static_values()
    values["actions"] = action_settings
    with pytest.raises(ValidationError):
        Settings.model_validate(values)

    wrong_caller_settings = {
        "url": "http://actions",
        "tls": {
            "ca_file": "/trust/agentplane.pem",
            "server_uri": ACTIONS_URI,
            "cert_file": "/tls/notifications.crt",
            "key_file": "/tls/notifications.key",
            "client_uri": ACTIONS_URI,
        },
    }
    wrong_caller_values = static_values()
    wrong_caller_values["actions"] = wrong_caller_settings
    with pytest.raises(ValidationError):
        Settings.model_validate(wrong_caller_values)

    server_only_values = static_values()
    server_only_values["history_service"] = {
        "target": "history:8080",
        "tls": {"ca_file": "/trust/agentplane.pem", "server_uri": HISTORY_SERVICE_URI},
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(server_only_values)


def test_service_operator_readers_decode() -> None:
    values = static_values()
    values["operator_reader_services"] = ["spiffe://agentplane/app"]
    values["rpc"] = {
        "listen_address": "0.0.0.0:8080",
        "auth": [{"mode": "spiffe", "ca_file": "/trust/workload-ca.pem"}],
        "tls": {"cert_file": "/tls/tls.crt", "key_file": "/tls/tls.key"},
    }

    settings = Settings.model_validate(values)

    assert settings.operator_reader_services == ["spiffe://agentplane/app"]


def test_generic_environment_does_not_override_static_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_DATABASE_URL", "postgresql://not-a-setting")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_QUOTAS__INBOXES_PER_ACCOUNT", "18")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_ACTIONS__URL", "http://override.invalid")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB_DELIVERY_RETENTION_DAYS", "3")
    values = static_values()
    values["quotas"] = {"inboxes_per_account": 12}
    values["github_delivery_retention_days"] = 14

    settings = Settings.model_validate(values)
    assert settings.quotas.inboxes_per_account == 12
    assert str(settings.actions.url) == "http://actions/"
    assert settings.github_delivery_retention_days == 14
    assert not hasattr(settings, "database_url")


@pytest.mark.parametrize(
    "api_url",
    [
        "not-a-url",
        "ftp://github.test",
        "https://user:password@github.test",
        "https://github.test?token=secret",
        "https://github.test#fragment",
    ],
)
def test_invalid_github_api_url(api_url: str) -> None:
    """The App JWT and installation tokens go here, so URL parts cannot be smuggled."""
    with pytest.raises(ValidationError):
        GitHubSettings.model_validate({"app_id": 42, "api_url": api_url})


def test_github_settings_are_static_and_credentials_are_outside_the_schema() -> None:
    settings = Settings.model_validate(
        {
            **static_values(),
            "github": {
                "app_id": 42,
                "api_url": "http://github.test/api/v3",
                "request_timeout_s": 12,
                "api_version": "2026-01-01",
            },
        }
    )
    assert settings.github is not None
    assert str(settings.github.api_url) == "http://github.test/api/v3"
    assert settings.github.request_timeout_s == 12
    assert settings.github.api_version == date(2026, 1, 1)

    with pytest.raises(ValidationError, match="Extra inputs"):
        GitHubSettings.model_validate({"app_id": 42, "private_key": "never-in-settings"})
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings.model_validate({**settings.model_dump(), "database_url": "never-in-settings"})


def test_explicit_null_retention_differs_from_omission() -> None:
    assert Settings.model_validate(static_values()).github_delivery_retention_days == 7
    assert (
        Settings.model_validate(
            {**static_values(), "github_delivery_retention_days": None}
        ).github_delivery_retention_days
        is None
    )


def test_receiver_requires_spiffe_trust() -> None:
    values = static_values()
    values["rpc"] = {
        "listen_address": "0.0.0.0:8080",
        "auth": [{"mode": "kubernetes_bearer", "audiences": ["agentplane-notifications"]}],
    }
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        Settings.model_validate(values)
