"""Deployment settings: YAML, nested environment overrides and invalid configuration."""

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.notification_service.settings import CONFIG_FILE_ENV, Settings


def test_yaml_settings_and_nested_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("""namespace: test
actions:
  url: http://actions
  token_file: /tokens/actions
sandbox_service:
  target: sandboxes:8080
  token_file: /tokens/sandboxes
""")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_DATABASE_URL", "postgresql://unused")
    settings = Settings(_cli_parse_args=False)
    assert settings.actions.url == "http://actions"
    assert settings.actions.token_file == Path("/tokens/actions")
    assert settings.sandbox_service.target == "sandboxes:8080"
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_ACTIONS__URL", "http://overridden-actions")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_SANDBOX_SERVICE__TARGET", "overridden-sandboxes:8080")
    settings = Settings(_cli_parse_args=False)
    assert settings.actions.url == "http://overridden-actions"
    assert settings.sandbox_service.target == "overridden-sandboxes:8080"
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    with config.open("a") as file:
        file.write("unknown_setting: true\n")
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings(_cli_parse_args=False)
    config.unlink()
    with pytest.raises(ValueError, match="regular file"):
        Settings(_cli_parse_args=False)


if __name__ == "__main__":
    pytest_bazel.main()
