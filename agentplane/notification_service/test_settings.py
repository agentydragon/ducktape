"""Deployment settings: YAML, nested environment overrides and invalid configuration."""

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.notification_service.settings import (
    CONFIG_FILE_ENV,
    GitHubSettings,
    NoticeDebounceSettings,
    SandboxServiceSettings,
    Settings,
)


def test_yaml_settings_and_nested_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("""namespace: test
actions:
  url: http://actions
  token_file: /tokens/actions
notice_debounce:
  quiet_seconds: 3
  max_wait_seconds: 15
sandbox_service:
  target: sandboxes:8080
  token_file: /tokens/sandboxes
""")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_DATABASE_URL", "postgresql://unused")
    settings = Settings(_cli_parse_args=False)
    assert settings.notice_debounce == NoticeDebounceSettings(quiet_seconds=3, max_wait_seconds=15)
    assert str(settings.actions.url) == "http://actions/"
    assert settings.actions.token_file == Path("/tokens/actions")
    assert settings.sandbox_service.target == "sandboxes:8080"
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    assert settings.sandbox_service.command_admission_timeout_s == 20
    assert settings.sandbox_service.request_timeout_s == 5
    assert settings.stale_inbox_confirmation_s == 30
    assert settings.sandbox_service.lifecycle_timeout_s == 310
    assert settings.sandbox_service.follow_timeout_s == 960
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_ACTIONS__URL", "http://overridden-actions")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_SANDBOX_SERVICE__TARGET", "overridden-sandboxes:8080")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_NOTICE_DEBOUNCE__QUIET_SECONDS", "0")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_SANDBOX_SERVICE__COMMAND_ADMISSION_TIMEOUT_S", "22")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_SANDBOX_SERVICE__REQUEST_TIMEOUT_S", "7")
    settings = Settings(_cli_parse_args=False)
    assert settings.notice_debounce == NoticeDebounceSettings(quiet_seconds=0, max_wait_seconds=15)
    assert str(settings.actions.url) == "http://overridden-actions/"
    assert settings.sandbox_service.target == "overridden-sandboxes:8080"
    assert settings.sandbox_service.command_admission_timeout_s == 22
    assert settings.sandbox_service.request_timeout_s == 7
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    with config.open("a") as file:
        file.write("unknown_setting: true\n")
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings(_cli_parse_args=False)
    config.unlink()
    with pytest.raises(ValueError, match="regular file"):
        Settings(_cli_parse_args=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_url", "not-a-url"),
        ("api_url", "ftp://github.test"),
        ("api_url", "https://user:password@github.test"),
        ("api_url", "https://github.test?token=secret"),
        ("api_url", "https://github.test#fragment"),
        ("request_timeout_s", 0),
        ("request_timeout_s", -1),
        ("request_timeout_s", float("nan")),
        ("request_timeout_s", float("inf")),
        ("api_version", "2026-99-99"),
        ("api_version", "2022-11-28\r\nInjected: value"),
    ],
)
def test_invalid_github_transport_settings(field: str, value: str | float) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings.model_validate(
            {"app_id": 42, "private_key": "fixture", "webhook_secret": "fixture-signing-secret", field: value}
        )


def test_github_transport_yaml_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("""namespace: test
actions:
  url: http://actions
  token_file: /tokens/actions
sandbox_service:
  target: sandboxes:8080
  token_file: /tokens/sandboxes
github:
  app_id: 42
  api_url: http://github.test/api/v3
  request_timeout_s: 12
  api_version: '2026-01-01'
""")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
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


@pytest.mark.parametrize(
    "values",
    [
        {"quiet_seconds": -1},
        {"quiet_seconds": float("nan")},
        {"quiet_seconds": float("inf")},
        {"max_wait_seconds": 0},
        {"max_wait_seconds": -1},
        {"max_wait_seconds": float("inf")},
    ],
)
def test_invalid_notice_debounce(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        NoticeDebounceSettings(**values)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_command_admission_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        SandboxServiceSettings(
            target="sandboxes:8080", token_file=Path("/tokens/sandboxes"), command_admission_timeout_s=timeout
        )


@pytest.mark.parametrize("name", ["request_timeout_s", "lifecycle_timeout_s", "follow_timeout_s"])
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_sandbox_service_deadline(name: str, timeout: float) -> None:
    with pytest.raises(ValidationError):
        SandboxServiceSettings.model_validate(
            {"target": "sandboxes:8080", "token_file": "/tokens/sandboxes", name: timeout}
        )


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_stale_confirmation_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "database_url": "postgresql://unused",
                "namespace": "test",
                "actions": {"url": "http://actions", "token_file": "/tokens/actions"},
                "sandbox_service": {"target": "sandboxes:8080", "token_file": "/tokens/sandboxes"},
                "stale_inbox_confirmation_s": timeout,
            }
        )


def test_notice_debounce_defaults() -> None:
    assert NoticeDebounceSettings().model_dump() == {"quiet_seconds": 2, "max_wait_seconds": 10}


if __name__ == "__main__":
    pytest_bazel.main()
