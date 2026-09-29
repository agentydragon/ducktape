"""Shared settings for the Plaid Link web app (//finance/plaid/link) and the sync
process (//finance/plaid/sync). Lives in the db layer because both consume it."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from finance.plaid.db.sync import SyncWindows

_NS_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
MAX_TRANSACTION_DAYS = 730


class PlaidSettings(BaseSettings):
    """Plaid and database settings shared by the Link app and sync process.

    OIDC credentials are deliberately absent so the scheduled sync never needs
    access to the web app's Authentik client secret or session key.
    """

    model_config = SettingsConfigDict(env_prefix="PLAID_MCP_")

    plaid_env: str = Field(description="Plaid environment: sandbox or production.")
    client_id: str
    client_secret: str
    database_url: str = Field(validation_alias="DATABASE_URL")
    public_base_url: str
    target_namespace: str | None = None
    managed_by: str = "plaid-mcp"
    host: str = "0.0.0.0"
    port: int = 8080
    transaction_days: int = Field(default=730, ge=1, le=MAX_TRANSACTION_DAYS)
    investment_transaction_days: int = Field(default=730, ge=1, le=MAX_TRANSACTION_DAYS)

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/link/callback"

    @property
    def namespace(self) -> str:
        if self.target_namespace is not None:
            return self.target_namespace
        if _NS_PATH.exists():
            return _NS_PATH.read_text().strip()
        return "plaid-mcp"

    @property
    def sync_windows(self) -> SyncWindows:
        return SyncWindows(
            transaction_days=self.transaction_days, investment_transaction_days=self.investment_transaction_days
        )


class PlaidWebSettings(PlaidSettings):
    """All Link app settings, including its confidential Authentik OIDC client."""

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_session_secret: SecretStr
    oidc_session_seconds: int = Field(default=28_800, gt=0)
