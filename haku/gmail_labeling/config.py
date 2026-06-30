"""Settings for the gmail_labeling MCP server."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.persistence import FilePersistence, PersistenceConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GMAIL_LABELING_", env_nested_delimiter="__")

    gmail_token_dir: Path = Field(
        description="Directory holding the Airlock-managed gmail.modify access token (files: access_token, expires_at), mounted from the gmail-modify-access-token secret."
    )
    allowed_prefix: str = Field(
        default="haku/",
        description="Managed label namespace; only labels whose name starts with this prefix are ever touched.",
    )
    static_bearer: str | None = Field(
        default=None,
        description="Machine bearer for /mcp (Haku's path). Accepted alongside the Authentik OAuth flow when `authentik` is also set; the sole gate when it isn't. Unset with no `authentik` leaves /mcp unauthenticated (local/dev only).",
    )
    authentik: AuthentikAuthConfig | None = Field(
        default=None,
        description="If set, also gate /mcp with an Authentik OAuth flow (for an interactive operator, e.g. claude.ai) on the same endpoint as `static_bearer`. Loads from GMAIL_LABELING_AUTHENTIK__* (oidc_issuer/client_id/client_secret/public_base_url).",
    )
    persistence: PersistenceConfig = Field(
        default=FilePersistence(),
        description="Backend for the OAuth flow's OIDCProxy state (DCR registrations, tokens); only used when `authentik` is set.",
    )
    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator("allowed_prefix")
    @classmethod
    def _non_empty_prefix(cls, value: str) -> str:
        if not value:
            raise ValueError("allowed_prefix must be non-empty")
        return value
