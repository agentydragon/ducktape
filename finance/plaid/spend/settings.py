"""Environment settings for the Plaid spend service."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SpendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAID_SPEND_")

    database_url: str = Field(validation_alias="DATABASE_URL")
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    api_oidc_issuer: str
    api_oidc_client_id: str
    api_oidc_discovered_issuer: str
    api_oidc_jwks_uri: str
    api_oidc_signing_algorithms: str = "RS256"

    browser_oidc_issuer: str
    browser_oidc_client_id: str
    browser_oidc_client_secret: SecretStr
    browser_oidc_session_secret: SecretStr
    browser_oidc_session_seconds: int = Field(default=28_800, gt=0)
    public_base_url: str

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def api_signing_algorithms(self) -> tuple[str, ...]:
        return tuple(
            algorithm.strip() for algorithm in self.api_oidc_signing_algorithms.split(",") if algorithm.strip()
        )
