"""Configuration of the Home Assistant component installer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import SettingsConfigDict

from util.yaml_file_settings import YamlFileSettings


class ComponentConfig(BaseModel):
    """Checksum-pinned release archive configuration."""

    version: str
    url: str
    sha256: str
    archive_path: str
    install_dir: str
    manifest_domain: str | None = None
    config_files: tuple[str, ...] = ()


class Config(BaseModel):
    """Complete settings-file schema, without runtime environment or file sources."""

    model_config = ConfigDict(extra="forbid")

    config_dir: Path = Field(description="Home Assistant's config directory, which holds `custom_components/`.")
    components: tuple[ComponentConfig, ...]


class Settings(YamlFileSettings, Config):
    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_COMPONENT_INSTALLER_", env_nested_delimiter="__")
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_COMPONENT_INSTALLER_CONFIG_FILE"

    # Keep BaseSettings' source-loading initializer as the runtime constructor; without this override,
    # mypy derives a required-field signature from Config.
    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
