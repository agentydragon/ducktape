"""Settings for the auth-aware Grocy MCP server."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig


class ServerSettings(BaseSettings):
    """Config for the Grocy MCP server."""

    model_config = SettingsConfigDict(env_prefix="GROCY_MCP_", env_nested_delimiter="__")

    auth: AuthentikAuthConfig | None = Field(
        default=None, description="Authentik auth config. None means no auth — MCP server and Grocy both unprotected."
    )

    grocy_url: str = Field(description="URL of the Grocy instance. For production this is the outpost-protected URL.")
    host: str = "0.0.0.0"
    port: int = 8765

    grocy_timeout: float = Field(default=30.0, description="Timeout (seconds) for Grocy API requests.")
    max_batch_size: int = Field(default=20, description="Maximum items per batch tool call.")
    max_concurrent_requests: int = Field(default=4, description="Maximum parallel Grocy API requests within a batch.")
    max_retries: int = Field(default=2, description="Retry count for transient errors (timeouts, 5xx).")
    retry_base_delay: float = Field(default=0.5, description="Initial retry delay in seconds; doubles each attempt.")
