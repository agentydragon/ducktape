"""Service settings: environment variables over the YAML file one of them names."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; Gazelle
# cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml


class YamlFileSettings(BaseSettings):
    """Environment variables override the YAML file that the `config_file_env` variable names, which is
    not a field and so not a flag. A named file that is not there is an error: pydantic-settings would
    skip it, and a bound deployment would run on defaults."""

    config_file_env: ClassVar[str]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(cls.config_file_env):
            if not Path(config_file).is_file():
                raise FileNotFoundError(f"{cls.config_file_env}={config_file} is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
