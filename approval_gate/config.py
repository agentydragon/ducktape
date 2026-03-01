"""Configuration for the approval gate server.

The bulk of settings are loaded from a YAML config file (CONFIG_PATH env var,
default /etc/approval-gate/config.yaml). Only secrets that must not live in
config files are read from environment variables.

Config file format (YAML):

  backends:
    exec:
      url: http://exec-backend:8766/mcp
      headers:
        Authorization: "Bearer ..."
    files:
      command: /usr/bin/file-server
      args: [--mcp]

  public_base_url: "https://approval-gate.example.com"
  operator_jwks_url: "https://auth.example.com/application/o/approval-gate/jwks/"
  db_path: "/data/approval_gate.db"      # optional, defaults to /data/approval_gate.db

Environment variables (secrets):
  AGENT_API_KEY — bearer token for agent/plugin MCP access (required)

Each backend entry matches fastmcp's MCPConfig mcpServers entry format.
Backend keys are validated as MCPMountPrefix (lowercase alphanumeric + underscore).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastmcp.mcp_config import MCPServerTypes
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp_infra.prefix import MCPMountPrefix


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_api_key: str
    backends: dict[MCPMountPrefix, MCPServerTypes]
    public_base_url: str
    db_path: Path = Path("/data/approval_gate.db")
    predicate_path: Path | None = Field(
        default=None,
        description=(
            "Path to a Python module exporting "
            "decide(server_namespace, tool_name, arguments) → Approved|Denied|NeedsHumanDecision."
        ),
    )
    operator_jwks_url: str
    host: str = "0.0.0.0"
    port: int = 8765

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @classmethod
    def load(cls) -> Settings:
        config_path = Path(os.environ.get("CONFIG_PATH", "/etc/approval-gate/config.yaml"))
        data = yaml.safe_load(config_path.read_text())
        data["agent_api_key"] = os.environ["AGENT_API_KEY"]
        return cls.model_validate(data)
