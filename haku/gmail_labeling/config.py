"""Settings for the gmail_labeling MCP server."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GMAIL_LABELING_")

    gmail_token_dir: Path = Field(
        description="Directory holding the Airlock-managed gmail.modify access token (files: access_token, expires_at), mounted from the gmail-modify-access-token secret."
    )
    allowed_prefix: str = Field(
        default="haku/",
        description="Managed label namespace; only labels whose name starts with this prefix are ever touched.",
    )
    static_bearer: str | None = Field(
        default=None,
        description="If set, require `Authorization: Bearer <token>` on /mcp. Unset means the endpoint is unauthenticated (local/dev only).",
    )
    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator("allowed_prefix")
    @classmethod
    def _non_empty_prefix(cls, value: str) -> str:
        if not value:
            raise ValueError("allowed_prefix must be non-empty")
        return value
