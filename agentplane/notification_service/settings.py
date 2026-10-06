"""YAML deployment configuration; GitHub secrets are supplied through Secret-backed environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# gazelle:include_dep @pypi//pyyaml

CONFIG_FILE_ENV = "AGENTPLANE_NOTIFICATIONS_CONFIG_FILE"
TOKEN_AUDIENCE = "agentplane-notifications"


class ActionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: str = Field(description="Base URL of the Actions Service's canonical request/event read API.")
    token_file: Path = Field(
        description="Path to the projected ServiceAccount token for Actions; reread for each request to follow rotation."
    )


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    app_id: int = Field(gt=0, description="Public numeric GitHub App ID; not a secret.")
    private_key: SecretStr = Field(
        min_length=1, description="PEM App private key, supplied through a Secret-backed environment variable."
    )
    webhook_secret: SecretStr = Field(
        min_length=16, description="Webhook HMAC signing secret, supplied through a Secret-backed environment variable."
    )
    max_body_bytes: int = Field(
        default=1024 * 1024, ge=1024, le=25 * 1024 * 1024, description="Maximum raw webhook request body size in bytes."
    )
    webhook_concurrency: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Maximum concurrent webhook requests per replica, held through durable commit; saturation returns 503.",
    )


class SandboxServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: str = Field(description="Sandbox Service gRPC host:port for session access and runner commands.")
    token_file: Path = Field(
        description="Path to the rotating projected ServiceAccount token used to authenticate to Sandbox Service."
    )


class NoticeDebounceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_seconds: float = Field(
        default=2,
        ge=0,
        le=3600,
        description="Wait this long after the newest unannounced inbox entry before preparing a runner notice. Zero disables debounce; persistence and inbox reads are never delayed.",
    )
    max_wait_seconds: float = Field(
        default=10,
        gt=0,
        le=3600,
        description="Cap the debounce wait from the oldest unannounced inbox entry, even during continuous traffic. Does not bound runner outages or delivery retries.",
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
        default=TOKEN_AUDIENCE, description="Audience required when TokenReview authenticates callers of this API."
    )
    notice_debounce: NoticeDebounceSettings = Field(default_factory=NoticeDebounceSettings)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    github: GitHubSettings | None = Field(
        default=None, description="GitHub source configuration. Disabled when absent from both YAML and environment."
    )

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
