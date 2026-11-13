"""Deployment contract for the standalone MCP edge."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.environment_runtime_transport.config import ClientTLS
from agentplane.mcp_edge.oauth_settings import OAuthSettings


class Settings(BaseModel):
    """Complete non-secret settings authored in the mounted YAML file."""

    model_config = ConfigDict(extra="forbid")

    config_file_env: ClassVar[str] = "AGENTPLANE_MCP_EDGE_CONFIG_FILE"
    actions_tls: ClientTLS | None = None

    actions_target: str = Field(min_length=1)
    policy_namespace: str = Field(min_length=1, description="Namespace holding the live Identity objects.")
    actions_token_file: str | None = Field(
        default=None, description="The MCP edge's rotating projected token for the Action Service audience."
    )
    oauth_file: Path | None = Field(
        default=None, description="Provider-owned OAuth settings projected from its Secret."
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535, description="The gRPC listener's port.")
    http_port: int = Field(
        default=8081, ge=1, le=65535, description="The HTTP listener's port: the facade, /mcp, OAuth and metrics."
    )
    operator_oidc: OperatorOidcSettings
    oauth: OAuthSettings | None = None
    max_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    direct_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ports(self) -> Settings:
        if self.port == self.http_port:
            raise ValueError("port and http_port must differ")
        if self.oauth_file is not None and self.oauth is not None:
            raise ValueError("oauth_file and inline oauth settings are mutually exclusive")
        return self
