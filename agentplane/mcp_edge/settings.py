"""Deployment contract for the standalone MCP edge."""

from __future__ import annotations

from collections.abc import Set
from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.mcp_edge.oauth_settings import OAuthSettings
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    """YAML settings with AGENTPLANE_MCP_EDGE_ environment overrides."""

    config_file_env: ClassVar[str] = "AGENTPLANE_MCP_EDGE_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_MCP_EDGE_", env_nested_delimiter="__", extra="forbid", hide_input_in_errors=True
    )

    database_url: str = Field(min_length=1)
    actions_target: str = Field(min_length=1)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535, description="The gRPC listener's port.")
    http_port: int = Field(
        default=8081, ge=1, le=65535, description="The HTTP listener's port: the facade, /mcp, OAuth and metrics."
    )
    caller_service_account_namespaces: Set[str] = Field(min_length=1)
    mintable_accounts: tuple[str, ...] = ()
    operator_oidc: OperatorOidcSettings
    oauth: OAuthSettings | None = None
    max_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    direct_wait_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_mintable_accounts(self) -> Settings:
        for account in self.mintable_accounts:
            namespace, separator, name = account.partition("/")
            if not separator or not name or "/" in name or namespace not in self.caller_service_account_namespaces:
                raise ValueError("mintable_accounts must be namespace/name within caller_service_account_namespaces")
        if self.oauth is not None and not self.mintable_accounts:
            raise ValueError("mintable_accounts must list an account when OAuth is enabled")
        return self
