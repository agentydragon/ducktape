"""Airlock's settings schema and configuration-file loader."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from airlock.oauth.config import OAuthConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)

    public_base_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_session_secret: SecretStr
    session_seconds: int = Field(default=28_800, gt=0)
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
        # Authentik credentials and the cookie signing key are provisioned by
        # tf/gitops/sso-providers and injected from a Kubernetes Secret.
        for field, env_var in (
            ("oidc_client_id", "AIRLOCK_OIDC_CLIENT_ID"),
            ("oidc_client_secret", "AIRLOCK_OIDC_CLIENT_SECRET"),
            ("oidc_session_secret", "AIRLOCK_OIDC_SESSION_SECRET"),
        ):
            if env_var in os.environ:
                data[field] = os.environ[env_var]
        return cls.model_validate(data)
