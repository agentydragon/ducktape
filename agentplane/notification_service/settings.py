"""YAML deployment configuration with environment overrides and rotating service tokens."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# gazelle:include_dep @pypi//pyyaml

CONFIG_FILE_ENV = "AGENTPLANE_NOTIFICATIONS_CONFIG_FILE"


class ActionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: str = Field(description="Base URL of the Actions Service's canonical request/event read API.")
    token_file: Path = Field(
        description="Path to the projected ServiceAccount token for Actions; reread for each request to follow rotation."
    )


class SandboxServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: str = Field(description="Sandbox Service gRPC host:port for session access and runner commands.")
    token_file: Path = Field(
        description="Path to the rotating projected ServiceAccount token used to authenticate to Sandbox Service."
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_NOTIFICATIONS_",
        env_nested_delimiter="__",
        extra="forbid",
        cli_parse_args=True,
        cli_kebab_case=True,
        hide_input_in_errors=True,
    )
    database_url: str = Field(description="PostgreSQL DSN supplied through a Secret-backed environment variable.")
    namespace: str = Field(description="Allowed caller ServiceAccount namespace and destination sandbox namespace.")
    actions: ActionsSettings
    sandbox_service: SandboxServiceSettings
    token_audience: str = Field(
        default="agentplane-egress",
        description=(
            "Audience required when TokenReview authenticates callers of this API. "
            "agentplane-egress is the shared first-party workload compatibility audience, "
            "not a notification routing setting; changing it requires coordinating caller token issuance."
        ),
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

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
            if not Path(config_file).is_file():
                raise ValueError("configured notification settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        return (*sources, file_secret_settings)
