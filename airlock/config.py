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
  db_path: "/data/airlock.db"      # optional, defaults to /data/airlock.db

Each backend entry matches fastmcp's MCPConfig mcpServers entry format.
Backend keys are validated as MCPMountPrefix (lowercase alphanumeric + underscore).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastmcp.mcp_config import MCPServerTypes, RemoteMCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from airlock.models import WaitMode, YieldAfterMs
from mcp_infra.prefix import MCPMountPrefix


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    backends: dict[MCPMountPrefix, MCPServerTypes]
    public_base_url: str
    db_path: Path = Path("/data/airlock.db")
    predicate_path: Path = Field(
        description=(
            "Path to a Python module exporting "
            "decide(server_namespace, tool_name, arguments) → Approved|Denied|NeedsHumanDecision."
        )
    )
    # -- Operator SPA OIDC (public client, Authorization Code + PKCE against Authentik) --
    oidc_issuer: str = Field(description="OIDC issuer URL for the operator SPA.")
    oidc_client_id: str = Field(description="Public client ID for the operator SPA (no secret).")

    # -- OIDCProxy upstream (confidential client for proxying DCR/OAuth flows) --
    oidc_client_secret: str = Field(description="Client secret for the OIDCProxy upstream confidential client.")
    oidc_upstream_issuer: str | None = Field(
        default=None,
        description="OIDC issuer for the upstream confidential client. "
        "Defaults to oidc_issuer when the same Authentik application serves both roles.",
    )
    oidc_upstream_client_id: str | None = Field(
        default=None,
        description="Client ID for the upstream confidential client. "
        "Defaults to oidc_client_id when the same application serves both roles.",
    )
    default_wait_mode: WaitMode = YieldAfterMs(timeout_ms=0)
    host: str = "0.0.0.0"
    port: int

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @classmethod
    def load(cls) -> Settings:
        config_path = Path(os.environ.get("CONFIG_PATH", "/etc/airlock/config.yaml"))
        data = yaml.safe_load(config_path.read_text())
        oidc_client_secret = os.environ.get("OIDC_CLIENT_SECRET")
        if oidc_client_secret:
            data["oidc_client_secret"] = oidc_client_secret
        settings = cls.model_validate(data)
        exec_token = os.environ.get("EXEC_BACKEND_TOKEN")
        if exec_token:
            for backend in settings.backends.values():
                if isinstance(backend, RemoteMCPServer) and "Authorization" not in backend.headers:
                    backend.headers["Authorization"] = f"Bearer {exec_token}"
        return settings
