"""Deployment pins for Action Service OAuth."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.models import OperatorPrincipal


class OAuthSettings(BaseModel):
    """Explicit deployment pins; no default issuer, operator mapping, or ephemeral keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_url: str
    upstream_client_id: str = Field(min_length=1)
    upstream_client_secret_file: Path
    base_url: str
    integration_app_url: str
    jwt_signing_key_file: Path
    encryption_key_file: Path
    upstream_issuer: str
    upstream_subject: str = Field(min_length=1)
    approving_operator: OperatorPrincipal
