"""Configuration for the Airlock server.

The bulk of settings are loaded from a YAML config file (CONFIG_PATH env var,
default /etc/airlock/config.yaml).

Config file format (YAML):

  backends:
    kubeapi_admin:
      url: http://kubeapi-admin-exec-mcp:8766/mcp
    files:
      command: /usr/bin/file-server
      args: [--mcp]

  public_base_url: "https://airlock.example.com"
  oidc_issuer: "https://auth.example.com/application/o/airlock/"

Required env vars (injected by Kubernetes, not in YAML):

  DATABASE_URL  — PostgreSQL connection URL from the CNPG airlock-db-app secret.
                  May be postgresql:// or postgresql+asyncpg://; the driver prefix
                  is normalised automatically.

Each backend entry matches fastmcp's MCPConfig mcpServers entry format.
Backend keys are validated as MCPMountPrefix (lowercase alphanumeric + underscore).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastmcp.mcp_config import MCPServerTypes, RemoteMCPServer
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from airlock.models import WaitMode, YieldAfterMs
from airlock.oauth.provider import GenericOAuth2Provider, OAuthConfig
from mcp_infra.prefix import MCPMountPrefix


class Settings(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)

    backends: dict[MCPMountPrefix, MCPServerTypes]
    public_base_url: str
    db_url: str = Field(
        validation_alias="DATABASE_URL",
        description="PostgreSQL connection URL, injected from DATABASE_URL env var (CNPG airlock-db-app secret).",
    )
    predicate_path: Path = Field(
        description=(
            "Path to a Python module exporting "
            "decide(server_namespace, tool_name, arguments) → Approved|Denied|NeedsHumanDecision."
        )
    )
    oidc_issuer: str
    oidc_client_id: str
    oidc_proxy_client_id: str | None = Field(
        default=None, description="OAuth client_id for OIDCProxy upstream auth. Enables MCP OAuth when set with secret."
    )
    oidc_proxy_client_secret: str | None = Field(
        default=None, description="OAuth client_secret for OIDCProxy upstream auth."
    )
    default_wait_mode: WaitMode = YieldAfterMs(timeout_ms=0)
    reconnect_interval_s: float = 30.0
    oauth: OAuthConfig = Field(description="OAuth token broker configuration")
    host: str = "0.0.0.0"
    port: int

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("db_url", mode="after")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        """Ensure the asyncpg driver prefix is present."""
        return v.replace("postgresql://", "postgresql+asyncpg://", 1)

    @classmethod
    def load(cls) -> Settings:
        config_path = Path(os.environ.get("CONFIG_PATH", "/etc/airlock/config.yaml"))
        data = yaml.safe_load(config_path.read_text())
        settings = cls.model_validate(data)
        exec_token = os.environ.get("EXEC_BACKEND_TOKEN")
        if exec_token:
            for backend in settings.backends.values():
                if isinstance(backend, RemoteMCPServer) and "Authorization" not in backend.headers:
                    backend.headers["Authorization"] = f"Bearer {exec_token}"
        return settings


def build_oauth_providers(oauth_config: OAuthConfig, default_redirect_uri: str) -> dict[str, GenericOAuth2Provider]:
    """Construct OAuth provider instances from config + env vars.

    `default_redirect_uri` is the shared callback URL used by any provider that does
    not set its own (legacy) `redirect_uri`.
    """
    providers: dict[str, GenericOAuth2Provider] = {}
    for p in oauth_config.providers:
        prefix = p.name.upper()
        client_id = os.environ[f"{prefix}_CLIENT_ID"]
        client_secret = os.environ[f"{prefix}_CLIENT_SECRET"]
        providers[p.name] = GenericOAuth2Provider(p, client_id, client_secret, default_redirect_uri)
    return providers
