"""Typed static deployment settings; runtime Secrets and environment loading belong to the Go process."""

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.notification_service.settings import (
    GitHubSettings,
    NoticeDebounceSettings,
    QuotaSettings,
    SessionDispatcherSettings,
    Settings,
)


def static_values() -> dict[str, object]:
    return {
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "auth": [{"mode": "kubernetes_bearer", "audiences": ["agentplane-notifications"]}],
        },
        "namespace": "test",
        "actions": {"url": "http://actions", "token_file": "/tokens/actions"},
        "runner_gateway": {
            "control_target": "{sandbox}-control.{namespace}.test:8080",
            "token_file": "/tokens/gateway",
        },
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


if __name__ == "__main__":
    pytest_bazel.main()
