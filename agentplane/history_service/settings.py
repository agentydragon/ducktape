"""History Service deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
from agentplane.subjects import ServiceAccountRef

CONFIG_FILE_ENV = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"


class SandboxServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, description="Sandbox Service gRPC host:port the ingester follows Sessions from.")
    token_file: Path = Field(
        description="Rotating projected ServiceAccount token for the Sandbox Service audience, reread on every call."
    )
    request_timeout_s: float = Field(default=15, gt=0, allow_inf_nan=False)
    follow_timeout_s: float = Field(
        default=960,
        gt=0,
        allow_inf_nan=False,
        description="Whole-stream safety deadline; keep above the Sandbox Service's follow_lease_s.",
    )
    grpc_channel_options: dict[str, int | str] = Field(
        default_factory=dict,
        description="gRPC options for this connection; receives replayed journal entries up to the configured limit.",
    )


class IngesterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description="Copy runner journals into the history tables. Off while the Sandbox Service ingester writes them.",
    )
    concurrency: int = Field(
        default=16, ge=1, le=64, description="Sessions followed at once per replica; the rest wait for a free slot."
    )
    retry_interval_s: float = Field(
        default=15,
        gt=0,
        allow_inf_nan=False,
        description="Delay before following again a Session that ended, failed or is claimed by another replica.",
    )


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
    sandbox_service: SandboxServiceSettings
    ingester: IngesterSettings = Field(default_factory=IngesterSettings)

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
