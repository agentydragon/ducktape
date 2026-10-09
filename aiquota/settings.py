"""The AIQuota API's deployment settings contract."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from the aiquota Deployment environment."""

    model_config = SettingsConfigDict(env_prefix="AIQUOTA_", extra="ignore")

    api_bearer_token: str
    config_path: Path = Field(default=Path("/etc/aiquota/config.toml"), validation_alias="AIQUOTA_CONFIG")
    cache_ttl_seconds: int = Field(default=120, ge=0)
    claude_proxy: str | None = None
    claude_proxy_ca: Path | None = None
    cli_proxy_api_key: SecretStr | None = Field(default=None, validation_alias="AIQUOTA_CLIPROXY_API_KEY")
    clickhouse_url: str | None = None
    clickhouse_username: str = "aiquota_ingest"
    clickhouse_password: SecretStr | None = None
    clickhouse_database: str = "aiquota"
    clickhouse_raw_table: str = "raw_http_observations"
    clickhouse_windows_table: str = "aiquota_windows"
    poll_interval_seconds: int = Field(default=300, gt=0)
    history_interval_seconds: int = Field(default=3600, gt=0)
    public_base_url: str = "https://aiquota.allegedly.works"
    oauth_issuer: str
    oauth_client_id: str
    oauth_client_secret: SecretStr
    oauth_session_secret: SecretStr
    oauth_username: str = "agentydragon"

    @model_validator(mode="after")
    def validate_clickhouse(self) -> Settings:
        if self.clickhouse_url and self.clickhouse_password is None:
            raise ValueError("AIQUOTA_CLICKHOUSE_PASSWORD is required when AIQUOTA_CLICKHOUSE_URL is set")
        return self

    @property
    def cache_ttl(self) -> timedelta:
        return timedelta(seconds=self.cache_ttl_seconds)
