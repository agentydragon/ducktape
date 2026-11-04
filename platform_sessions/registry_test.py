"""A synthetic integration exercises the registry without provider credentials."""

from pathlib import PurePosixPath

import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, ValidationError

from platform_sessions.registry import IntegrationConfigError, IntegrationRegistry, IntegrationType


class ExampleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str = "default"


@pytest.fixture
def registry() -> IntegrationRegistry:
    return IntegrationRegistry((IntegrationType("example", ExampleSettings, "https://example.test"),))


def test_resolves_multiple_instances_and_type_settings(registry: IntegrationRegistry) -> None:
    result = registry.resolve(
        {
            "integrations": [
                {"id": "first", "type": "example"},
                {
                    "id": "second",
                    "type": "example",
                    "enabled": False,
                    "state_dir": "/srv/second",
                    "upstream_base_url": "http://example.test:8080",
                    "poll_interval_seconds": 120,
                    "settings": {"mode": "other"},
                },
            ]
        }
    )
    assert result[0].upstream_base_url == "https://example.test"
    assert result[0].state_dir == PurePosixPath("/data/first")
    assert result[0].settings == ExampleSettings()
    assert result[0].poll_interval_seconds == 900
    assert result[1].settings == ExampleSettings(mode="other")
    assert result[1].poll_interval_seconds == 120
    assert result[1].state_dir == PurePosixPath("/srv/second")
    assert result[1].upstream_base_url == "http://example.test:8080"
    assert not result[1].enabled


@pytest.mark.parametrize(
    "bad",
    [
        {"integrations": [{"id": "../x", "type": "example"}]},
        {"integrations": [{"id": "example\n", "type": "example"}]},
        {"integrations": [{"id": "a", "type": "example"}, {"id": "a", "type": "example"}]},
        {"integrations": [{"id": "a", "type": "missing"}]},
        {"integrations": [{"id": "a", "type": "example", "enabled": "yes"}]},
        {"integrations": [{"id": "a", "type": "example", "poll_interval_seconds": 0}]},
        {"integrations": [{"id": "a", "type": "example", "poll_interval_seconds": "900"}]},
        {"integrations": [{"id": "a", "type": "example", "state_dir": "relative"}]},
        {"integrations": [{"id": "a", "type": "example", "state_dir": ""}]},
        {"integrations": [{"id": "a", "type": "example", "upstream_base_url": ""}]},
        {"integrations": [{"id": "a", "type": "example", "state_dir": "/data/../other"}]},
        {
            "integrations": [
                {"id": "a", "type": "example", "state_dir": "/data/shared"},
                {"id": "b", "type": "example", "state_dir": "/data/shared"},
            ]
        },
        {"integrations": [{"id": "a", "type": "example", "upstream_base_url": "https://user:secret@example.test"}]},
        {"integrations": [{"id": "a", "type": "example", "upstream_base_url": "https://example.test/?token=secret"}]},
        {"integrations": [{"id": "a", "type": "example", "settings": {"unexpected": True}}]},
    ],
)
def test_invalid_configuration_is_rejected_without_echoing_values(registry: IntegrationRegistry, bad: object) -> None:
    with pytest.raises((IntegrationConfigError, ValidationError)) as error:
        registry.resolve(bad)
    assert "secret" not in str(error.value)


def test_host_validation_error_identifies_invalid_field(registry: IntegrationRegistry) -> None:
    with pytest.raises(ValidationError) as error:
        registry.resolve({"integrations": [{"id": "bad/id", "type": "example"}]})
    assert error.value.errors()[0]["loc"] == ("integrations", 0, "id")


def test_provider_validation_error_identifies_invalid_setting(registry: IntegrationRegistry) -> None:
    with pytest.raises(ValidationError) as error:
        registry.resolve({"integrations": [{"id": "a", "type": "example", "settings": {"mode": 5}}]})
    assert error.value.errors()[0]["loc"] == ("mode",)


def test_duplicate_types_are_rejected() -> None:
    item = IntegrationType("example", ExampleSettings, "https://example.test")
    with pytest.raises(IntegrationConfigError):
        IntegrationRegistry((item, item))


if __name__ == "__main__":
    pytest_bazel.main()
