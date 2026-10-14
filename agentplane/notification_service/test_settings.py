"""Deployment settings: YAML, nested environment overrides and invalid configuration."""

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

BLOCKS: dict[str, object] = {
    "actions": {"url": "http://actions", "token_file": "/tokens/actions"},
    "runner_gateway": {
        "target": "gateway:8080",
        "control_target": "{sandbox}-control.{namespace}.test:8080",
        "token_file": "/tokens/gateway",
    },
    "history_service": {"target": "history:8080", "token_file": "/tokens/history"},
}


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal valid settings YAML, selected through the config-file variable; tests append sections."""
    path = tmp_path / "settings.yaml"
    path.write_text("""namespace: test
actions:
  url: http://actions
  token_file: /tokens/actions
runner_gateway:
  target: gateway:8080
  control_target: "{sandbox}-control.{namespace}.test:8080"
  token_file: /tokens/gateway
history_service:
  target: history:8080
  token_file: /tokens/history
session_dispatcher:
  target: dispatcher:8080
  token_file: /tokens/dispatcher
""")
    monkeypatch.setenv(Settings.config_file_env, str(path))
    return path


def test_yaml_settings_and_nested_environment_overrides(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with settings_file.open("a") as file:
        file.write("""quotas:
  inboxes_per_account: 12
  active_subscriptions_per_inbox: 24
  entries_per_inbox: 500
notice_debounce:
  quiet_seconds: 3
  max_wait_seconds: 15
""")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_DATABASE_URL", "postgresql://unused")
    settings = Settings(_cli_parse_args=False)
    assert settings.quotas == QuotaSettings(
        inboxes_per_account=12, active_subscriptions_per_inbox=24, entries_per_inbox=500
    )
    assert settings.notice_debounce == NoticeDebounceSettings(quiet_seconds=3, max_wait_seconds=15)
    assert str(settings.actions.url) == "http://actions/"
    assert settings.actions.token_file == Path("/tokens/actions")
    assert settings.runner_gateway.command_admission_timeout_s == 20
    assert settings.session_dispatcher == SessionDispatcherSettings(
        target="dispatcher:8080", token_file=Path("/tokens/dispatcher"), request_timeout_s=15
    )
    assert settings.stale_inbox_confirmation_s == 30
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_QUOTAS__INBOXES_PER_ACCOUNT", "18")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_QUOTAS__ACTIVE_SUBSCRIPTIONS_PER_INBOX", "36")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_QUOTAS__ENTRIES_PER_INBOX", "750")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_ACTIONS__URL", "http://overridden-actions")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_NOTICE_DEBOUNCE__QUIET_SECONDS", "0")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_RUNNER_GATEWAY__COMMAND_ADMISSION_TIMEOUT_S", "22")
    settings = Settings(_cli_parse_args=False)
    assert settings.quotas == QuotaSettings(
        inboxes_per_account=18, active_subscriptions_per_inbox=36, entries_per_inbox=750
    )
    assert settings.notice_debounce == NoticeDebounceSettings(quiet_seconds=0, max_wait_seconds=15)
    assert str(settings.actions.url) == "http://overridden-actions/"
    assert settings.runner_gateway.command_admission_timeout_s == 22
    with settings_file.open("a") as file:
        file.write("unknown_setting: true\n")
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings(_cli_parse_args=False)


def test_dispatcher_configuration_is_required_and_complete(settings_file: Path) -> None:
    configured = settings_file.read_text()
    settings_file.write_text(
        configured.replace("session_dispatcher:\n  target: dispatcher:8080\n  token_file: /tokens/dispatcher\n", "")
    )
    with pytest.raises(ValidationError, match="Field required"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)

    settings_file.write_text(configured.replace("  token_file: /tokens/dispatcher", ""))
    with pytest.raises(ValidationError, match="Field required"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)

    settings_file.write_text(
        configured.replace(
            "  token_file: /tokens/dispatcher", "  token_file: /tokens/dispatcher\n  request_timeout_s: 8"
        )
    )
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert settings.session_dispatcher == SessionDispatcherSettings(
        target="dispatcher:8080", token_file=Path("/tokens/dispatcher"), request_timeout_s=8
    )


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
    """The App JWT and installation tokens go to this URL, so `HttpEndpointUrl` refuses smuggled parts."""
    with pytest.raises(ValidationError):
        GitHubSettings.model_validate(
            {"app_id": 42, "private_key": "fixture", "webhook_secret": "fixture-signing-secret", "api_url": api_url}
        )


def test_github_transport_yaml_and_environment(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with settings_file.open("a") as file:
        file.write("""github:
  app_id: 42
  api_url: http://github.test/api/v3
  request_timeout_s: 12
  api_version: '2026-01-01'
""")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY", "fixture")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", "fixture-signing-secret")
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert settings.github is not None
    assert str(settings.github.api_url) == "http://github.test/api/v3"
    assert settings.github.request_timeout_s == 12
    assert settings.github.api_version == date(2026, 1, 1)
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__API_URL", "http://localhost:9999/mock/")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__REQUEST_TIMEOUT_S", "2.5")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__API_VERSION", "2022-11-28")
    overridden = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert overridden.github is not None
    assert str(overridden.github.api_url) == "http://localhost:9999/mock/"
    assert overridden.github.request_timeout_s == 2.5
    assert overridden.github.api_version == date(2022, 11, 28)


def test_github_presence_and_secret_separation(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file.write_text(settings_file.read_text() + "github: null\n")
    assert Settings(database_url="postgresql://unused", _cli_parse_args=False).github is None
    settings_file.write_text(settings_file.read_text().replace("github: null\n", ""))
    assert Settings(database_url="postgresql://unused", _cli_parse_args=False).github is None
    # A present mapping enables GitHub and must be complete; secrets overlay public YAML.
    with settings_file.open("a") as file:
        file.write("github:\n  app_id: 42\n")
    with pytest.raises(ValidationError, match="Field required"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)
    private = "fixture-private-key\nmultiline"
    secret = "test-signing-key"  # 16-character minimum.
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY", private)
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", secret)
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert settings.github is not None
    assert settings.github.app_id == 42
    assert settings.github.private_key.get_secret_value() == private
    assert settings.github.webhook_secret.get_secret_value() == secret
    assert private not in repr(settings)
    assert secret not in settings.model_dump_json()
    # Nested environment fields contribute presence even when YAML says null.
    settings_file.write_text(settings_file.read_text().replace("github:\n  app_id: 42\n", "github: null\n"))
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__APP_ID", "42")
    assert Settings(database_url="postgresql://unused", _cli_parse_args=False).github is not None
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", "test-signing-ke")
    with pytest.raises(ValidationError) as failure:
        Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert "test-signing-ke" not in str(failure.value)


if __name__ == "__main__":
    pytest_bazel.main()
