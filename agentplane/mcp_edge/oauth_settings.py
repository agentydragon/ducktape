"""Deployment pins for the MCP edge's external OAuth server."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class OperatorPrincipal(BaseModel):
    """The operator identity whose upstream sign-in may approve an external Connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)


class OAuthSettings(BaseModel):
    """Explicit deployment pins; no default issuer, operator mapping, or ephemeral keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream_client_id: str = Field(min_length=1)
    upstream_client_secret_file: Path
    base_url: str
    integration_app_url: str
    upstream_issuer: str
    upstream_subject: str = Field(min_length=1)
    approving_operator: OperatorPrincipal
