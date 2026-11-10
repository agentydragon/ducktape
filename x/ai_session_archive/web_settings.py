"""Settings for the owner-authenticated workstation session archive viewer."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AI_SESSION_ARCHIVE_")

    database_url: SecretStr
    frontend_dir: Path | None = None
    public_base_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_session_secret: SecretStr
    oidc_allowed_subject: str
    oidc_session_seconds: int = Field(default=28_800, gt=0)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
