from pathlib import Path

import pytest
import pytest_bazel
import settings
from test_support import TEST_SETTINGS


def test_load_settings_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "provisioner.yaml"
    config.write_text(
        """
home_assistant_url: http://configured.test:8123
client_id: https://configured.test/
redirect_uri: https://configured.test/
username: configured-admin
display_name: Configured Administrator
required_onboarding_steps: [user, core_config]
http_config:
  server_host: [127.0.0.1]
  server_port: 8124
  cors_allowed_origins: [https://cast.test]
  use_x_forwarded_for: true
  trusted_proxies: [127.0.0.1/32]
  login_attempts_threshold: -1
  ip_ban_enabled: true
  ssl_profile: modern
  use_x_frame_options: true
components:
  - version: 2.2.1
    url: https://example.test/component.zip
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    archive_path: "*/custom_components/example"
    install_dir: example
    manifest_domain: example
    config_files: []
  - version: 1.1.1
    url: https://example.test/root-component.zip
    sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    archive_path: .
    install_dir: root_component
    manifest_domain: null
    config_files: [automations.yaml, scripts.yaml, scenes.yaml]
onboarding_enabled: false
"""
    )
    monkeypatch.setenv(settings.ProvisionerSettings.config_file_env, str(config))

    loaded = settings.load_settings()

    assert loaded.home_assistant_url == "http://configured.test:8123"
    assert loaded.http_config.server_port == 8124
    assert loaded.components[0].archive_path == "*/custom_components/example"
    assert loaded.components[1].config_files == ("automations.yaml", "scripts.yaml", "scenes.yaml")
    assert loaded.onboarding_enabled is False


def test_test_settings_are_complete() -> None:
    assert [component.install_dir for component in TEST_SETTINGS.components] == ["example", "root_component"]
    assert TEST_SETTINGS.onboarding_enabled is True


if __name__ == "__main__":
    pytest_bazel.main()
