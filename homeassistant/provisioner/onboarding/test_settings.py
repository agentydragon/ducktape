"""How the Job's settings reach the model: its file declares the time zone, and the optional location
Secret's env vars complete the same nested `core_config` rather than replacing it."""

import pytest
import pytest_bazel
import yaml

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.onboarding.settings import HomeLocation, Settings

LATITUDE_ENV = "HOME_ASSISTANT_ONBOARDING_CORE_CONFIG__LOCATION__LATITUDE"
LONGITUDE_ENV = "HOME_ASSISTANT_ONBOARDING_CORE_CONFIG__LOCATION__LONGITUDE"


@pytest.fixture
def settings_file(tmp_path, monkeypatch, endpoint: HomeAssistantEndpoint):
    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "endpoint": endpoint.model_dump(),
                "owner_username": "test-admin",
                "owner_display_name": "Test Administrator",
                "http_config": {
                    "server_host": ["127.0.0.1"],
                    "server_port": 8124,
                    "cors_allowed_origins": [],
                    "use_x_forwarded_for": True,
                    "trusted_proxies": ["127.0.0.1/32"],
                    "login_attempts_threshold": -1,
                    "ip_ban_enabled": True,
                    "ssl_profile": "modern",
                    "use_x_frame_options": True,
                },
                "core_config": {"time_zone": "Etc/GMT+5"},
            }
        )
    )
    monkeypatch.setenv(Settings.config_file_env, str(path))
    monkeypatch.setenv("HOME_ASSISTANT_ONBOARDING_OWNER_PASSWORD", "secret-password")
    monkeypatch.delenv(LATITUDE_ENV, raising=False)
    monkeypatch.delenv(LONGITUDE_ENV, raising=False)
    return path


def test_the_location_secret_completes_the_files_core_config(settings_file, monkeypatch):
    monkeypatch.setenv(LATITUDE_ENV, "12.5")
    monkeypatch.setenv(LONGITUDE_ENV, "-45.25")
    core_config = Settings().core_config
    assert core_config.time_zone == "Etc/GMT+5"
    assert core_config.location == HomeLocation(latitude=12.5, longitude=-45.25)


def test_without_the_secret_the_location_is_not_managed(settings_file):
    assert Settings().core_config.location is None


if __name__ == "__main__":
    pytest_bazel.main()
