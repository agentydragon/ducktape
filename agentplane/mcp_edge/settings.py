"""Deployment contract for the standalone MCP edge."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from agentplane.net.peertls.config import ClientTLS, ServerTLSListener, ServiceSPIFFEID


class Settings(BaseModel):
    """Complete non-secret settings authored in the mounted YAML file."""

    model_config = ConfigDict(extra="forbid")

    config_file_env: ClassVar[str] = "AGENTPLANE_MCP_EDGE_CONFIG_FILE"
    actions_tls: ClientTLS | None = None
    rpc_tls: ServerTLSListener | None = Field(
        default=None, description="Optional additional gRPC listener with a workload SVID."
    )
    operator_services: list[ServiceSPIFFEID] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description="Exact service SVIDs allowed on rpc_tls; requires rpc_tls.client_ca_file.",
    )

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
        if self.rpc_tls is not None and self.rpc_tls.listen in {
            f"{self.host}:{self.port}",
            f"{self.host}:{self.http_port}",
        }:
            raise ValueError("rpc_tls.listen must differ from port and http_port")
        if len(set(self.operator_services)) != len(self.operator_services):
            raise ValueError("operator_services must not contain duplicates")
        if self.operator_services and (self.rpc_tls is None or self.rpc_tls.client_ca_file is None):
            raise ValueError("operator_services requires rpc_tls.client_ca_file")
        if self.rpc_tls is not None and self.rpc_tls.client_ca_file is not None and not self.operator_services:
            raise ValueError("rpc_tls.client_ca_file requires operator_services")
        if self.oauth_file is not None and self.oauth is not None:
            raise ValueError("oauth_file and inline oauth settings are mutually exclusive")
        return self
