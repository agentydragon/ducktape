"""Deployment contract for the standalone MCP edge."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.net.peertls.config import MCP_EDGE_URI, ClientTLS, ServerTLSListener, ServiceSPIFFEID


class Settings(BaseModel):
    """Complete non-secret settings authored in the mounted YAML file."""

    model_config = ConfigDict(extra="forbid")

    config_file_env: ClassVar[str] = "AGENTPLANE_MCP_EDGE_CONFIG_FILE"
    actions_tls: ClientTLS | None = None
    rpc_tls: ServerTLSListener = Field(description="The gRPC listener using the edge's workload SVID.")
    operator_services: list[ServiceSPIFFEID] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description="Exact service SVIDs allowed on rpc_tls; requires rpc_tls.client_ca_file.",
    )

    actions_target: str = Field(min_length=1)
    policy_namespace: str = Field(min_length=1, description="Namespace holding the live Identity objects.")
    actions_token_file: str | None = Field(
        default=None, description="The MCP edge's projected token when it uses bearer authentication to Action Service."
    )
    oauth_file: Path | None = Field(
        default=None, description="Provider-owned OAuth settings projected from its Secret."
    )
    host: str = "0.0.0.0"
    http_port: int = Field(
        default=8081,
        ge=1,
        le=65535,
        description="The HTTP listener's port: the facade, /mcp, OAuth, health and metrics.",
    )
    operator_oidc: OperatorOidcSettings
    oauth: OAuthSettings | None = None
    max_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    direct_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ports(self) -> Settings:
        if self.rpc_tls.listen == f"{self.host}:{self.http_port}":
            raise ValueError("rpc_tls.listen must differ from http_port")
        if len(set(self.operator_services)) != len(self.operator_services):
            raise ValueError("operator_services must not contain duplicates")
        if self.operator_services and self.rpc_tls.client_ca_file is None:
            raise ValueError("operator_services requires rpc_tls.client_ca_file")
        if self.rpc_tls.client_ca_file is not None and not self.operator_services:
            raise ValueError("rpc_tls.client_ca_file requires operator_services")
        if self.oauth_file is not None and self.oauth is not None:
            raise ValueError("oauth_file and inline oauth settings are mutually exclusive")
        if self.oauth_file is not None or self.oauth is not None:
            if self.actions_tls is None:
                if self.actions_token_file is None or not self.actions_token_file.strip():
                    raise ValueError("OAuth requires an Action caller credential")
            else:
                self.actions_tls.validate_caller(self.actions_token_file, MCP_EDGE_URI)
        return self
