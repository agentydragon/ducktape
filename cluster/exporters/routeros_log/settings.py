from __future__ import annotations

from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTEROS_LOG_", case_sensitive=False, extra="ignore", frozen=True)

    host: str = Field(description="The device's address; its api-ssl certificate must name it.")
    port: int = Field(default=8729, description="api-ssl port.")
    username: str = Field(description="A RouterOS user with the `api` and `read` policies.")
    password: SecretStr
    ca_file: Path = Field(description="CA bundle the device's api-ssl certificate verifies against.")
    loki_push_url: HttpUrl = Field(description="Loki's `/loki/api/v1/push` endpoint.")
    labels: dict[str, str] = Field(
        description='Stream labels on every line, as JSON (`{"job": "..."}`); `topics` is added per line.'
    )
    poll_interval_seconds: float = Field(default=30, gt=0, description="Start of one poll to the start of the next.")
    timeout_seconds: float = Field(default=30, gt=0, description="Per API call and per Loki push.")
    listen_port: int = Field(default=9174, description="Port serving `/metrics`.")
