"""Settings for the Authentik MCP POC.

Pure environment-variable config — intentionally minimal. The POC runs two
separate processes via separate entrypoints: the FastMCP server (`server.py`)
and the FastAPI whoami backend (`backend.py`). Server settings use the
`AUTHENTIK_MCP_POC_` prefix; backend settings use the
`AUTHENTIK_MCP_POC_BACKEND_` prefix.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """Config for the FastMCP server (the client-facing side)."""

    model_config = SettingsConfigDict(env_prefix="AUTHENTIK_MCP_POC_")

    oidc_issuer: str = Field(
        description="Authentik OIDC issuer URL, e.g. https://auth.allegedly.works/application/o/authentik-mcp-poc/"
    )
    oidc_client_id: str = Field(description="OAuth2 client_id for the Authentik OAuth2 provider")
    oidc_client_secret: str = Field(description="OAuth2 client_secret for the Authentik OAuth2 provider")
    public_base_url: str = Field(
        description="Public URL at which this MCP server is reachable, e.g. https://authentik-mcp-poc.allegedly.works"
    )
    backend_url: str = Field(
        description="Public URL of the Authentik-proxy-protected whoami backend, e.g. "
        "https://authentik-mcp-poc-backend.allegedly.works"
    )
    host: str = "0.0.0.0"
    port: int = 8765

    def normalized_public_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    def normalized_issuer(self) -> str:
        return self.oidc_issuer.rstrip("/")


class BackendSettings(BaseSettings):
    """Config for the whoami backend (the outpost-protected side)."""

    model_config = SettingsConfigDict(env_prefix="AUTHENTIK_MCP_POC_BACKEND_")

    host: str = "0.0.0.0"
    port: int = 8080
