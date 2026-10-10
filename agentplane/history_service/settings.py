"""History Service deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
from agentplane.subjects import ServiceAccountRef

CONFIG_FILE_ENV = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_HISTORY_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    database_url: str = Field(min_length=1)
    reader_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1)
    request_timeout_s: float = Field(default=15, gt=0, le=60)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    health_port: int = Field(default=8081, ge=1, le=65535)
    kubeconfig: Path | None = None

    def __init__(self, **values: Any) -> None:
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
        sources = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
