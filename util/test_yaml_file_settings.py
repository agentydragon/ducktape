from pathlib import Path
from typing import ClassVar

import pytest
import pytest_bazel
from pydantic_settings import SettingsConfigDict

from util.yaml_file_settings import YamlFileSettings


class ExampleSettings(YamlFileSettings):
    config_file_env: ClassVar[str] = "EXAMPLE_TEST_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="EXAMPLE_TEST_")

    name: str
    replicas: int = 1


def test_environment_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("name: from-file\nreplicas: 3\n")
    monkeypatch.setenv(ExampleSettings.config_file_env, str(config))
    monkeypatch.setenv("EXAMPLE_TEST_NAME", "from-env")

    assert ExampleSettings() == ExampleSettings(name="from-env", replicas=3)


def test_a_named_file_that_is_not_there_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings skips an absent YAML file, which would leave a bound deployment on defaults."""
    monkeypatch.setenv(ExampleSettings.config_file_env, str(tmp_path / "never-written.yaml"))
    monkeypatch.setenv("EXAMPLE_TEST_NAME", "from-env")

    with pytest.raises(FileNotFoundError):
        ExampleSettings()


if __name__ == "__main__":
    pytest_bazel.main()
