"""Configuration for the Airlock OAuth credential broker."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from airlock.oauth.provider import GenericOAuth2Provider, OAuthConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)

    public_base_url: str
    oidc_issuer: str
    oidc_client_id: str
    oauth: OAuthConfig = Field(description="OAuth token broker configuration")
    host: str = "0.0.0.0"
    port: int

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @classmethod
    def load(cls) -> Settings:
        config_path = Path(os.environ.get("CONFIG_PATH", "/etc/airlock/config.yaml"))
        data = yaml.safe_load(config_path.read_text())
        return cls.model_validate(data)


def build_oauth_providers(oauth_config: OAuthConfig, default_redirect_uri: str) -> dict[str, GenericOAuth2Provider]:
    """Construct OAuth provider instances from config + env vars.

    `default_redirect_uri` is the shared callback URL used by any provider that does
    not set its own (legacy) `redirect_uri`.
    """
    providers: dict[str, GenericOAuth2Provider] = {}
    for p in oauth_config.providers:
        prefix = p.name.upper()
        client_id = os.environ[f"{prefix}_CLIENT_ID"]
        client_secret = os.environ[f"{prefix}_CLIENT_SECRET"]
        providers[p.name] = GenericOAuth2Provider(p, client_id, client_secret, default_redirect_uri)
    return providers
