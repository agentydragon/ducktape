"""Environment settings for the Plaid spend service."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SpendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAID_SPEND_")

    database_url: str = Field(validation_alias="DATABASE_URL")
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    cards_config_path: Path = Path("/etc/plaid-spend/cards.json")

    api_oidc_issuer: str
    api_oidc_client_id: str
    api_oidc_discovered_issuer: str
    api_oidc_jwks_uri: str
    api_oidc_signing_algorithms: str = "RS256"
    web_oidc_issuer: str
    web_oidc_public_base_url: str
    web_oidc_client_id: SecretStr
    web_oidc_client_secret: SecretStr
    web_oidc_session_secret: SecretStr
    web_oidc_session_seconds: int = Field(default=28_800, gt=0)

    @property
    def api_signing_algorithms(self) -> tuple[str, ...]:
        return tuple(
            algorithm.strip() for algorithm in self.api_oidc_signing_algorithms.split(",") if algorithm.strip()
        )
