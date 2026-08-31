"""Haku Console's complete Pydantic settings graph."""

from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from haku.console.config import ConsoleProcessConfig
from haku.console.mcp_config import ConsoleConfigFile


class _ConfigFileSettings(BaseSettings):
    """The one setting needed before the YAML source itself can be constructed."""

    model_config = SettingsConfigDict(extra="ignore")

    config_file: Path = Field(validation_alias="HAKU_CONSOLE_CONFIG_FILE")


class Settings(ConsoleConfigFile, ConsoleProcessConfig, BaseSettings):
    """The complete deploy catalog, with environment values overlaid on its typed leaves."""

    model_config = SettingsConfigDict(
        env_prefix="HAKU_CONSOLE__", env_nested_delimiter="__", extra="forbid", populate_by_name=True
    )

    def __init__(self, **values: Any) -> None:
        # BaseSettings accepts an empty constructor and fills required fields from its sources;
        # spell that dynamic boundary explicitly because mypy derives a required-field signature
        # from the two typed BaseModel parents.
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load the complete YAML below constructor, environment, and dotenv overrides."""
        config_file = init_settings().get("config_file")
        if config_file is None:
            config_file = _ConfigFileSettings().config_file
        config_file = Path(config_file)
        if not config_file.is_file():
            raise RuntimeError(f"Console config file does not exist: {config_file}")
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=config_file),
            file_secret_settings,
        )
