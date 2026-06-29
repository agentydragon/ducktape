"""Settings for the gmail_labeling MCP server."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GMAIL_LABELING_")

    gmail_token_file: Path = Field(
        description="Path to the authorized-user OAuth token JSON (gmail.modify scope), provisioned and rotated by Airlock."
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
