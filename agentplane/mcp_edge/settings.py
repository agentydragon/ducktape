"""Deployment contract for the standalone MCP edge."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.environment_runtime_transport.config import ClientTLS
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    """YAML settings with AGENTPLANE_MCP_EDGE_ environment overrides."""

    actions_tls: ClientTLS | None = None

    config_file_env: ClassVar[str] = "AGENTPLANE_MCP_EDGE_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_MCP_EDGE_", env_nested_delimiter="__", extra="forbid", hide_input_in_errors=True
    )

    database_url: str = Field(min_length=1)
    actions_target: str = Field(min_length=1)
    policy_namespace: str = Field(min_length=1, description="Namespace holding the live Identity objects.")
    actions_token_file: str | None = Field(
        default=None, description="The MCP edge's rotating projected token for the Action Service audience."
    )
    # Accepted only while the first Identity cutover rollout replaces rendered settings. These values have no active
    # authorization or token-minting effect and are removed after the rollout.
    caller_service_account_namespaces: list[str] | None = None
    mintable_accounts: list[str] | None = None
    connection_identities: dict[str, Any] | None = None
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
        return self
