"""Configuration of the Home Assistant component installer."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from homeassistant.provisioner.yaml_settings import YamlFileSettings


class ComponentConfig(BaseModel):
    """Checksum-pinned release archive configuration."""

    version: str
    url: str
    sha256: str
    archive_path: str
    install_dir: str
    manifest_domain: str | None = None
    config_files: tuple[str, ...] = ()


class Settings(YamlFileSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_COMPONENT_INSTALLER_", env_nested_delimiter="__")
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_COMPONENT_INSTALLER_CONFIG_FILE"

    config_dir: Path = Field(description="Home Assistant's config directory, which holds `custom_components/`.")
    components: tuple[ComponentConfig, ...]
