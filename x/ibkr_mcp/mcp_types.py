"""Server settings for the IBKR market-data MCP server.

Non-secret structured config (gateway URL, auth issuer/URLs/direct_jwt_trusts,
persistence) is loaded from the YAML file at ``IBKR_MCP_CONFIG_FILE``; secrets
(auth ``oidc_client_secret``) stay in ``IBKR_MCP_*`` env from a k8s Secret. Env
outranks the file and the two are deep-merged, so the nested ``auth`` model draws
its non-secret fields from YAML and its secret fields from env.
"""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.persistence import FilePersistence, PersistenceConfig


class ServerSettings(BaseSettings):
    """Config for the IBKR market-data MCP server."""

    model_config = SettingsConfigDict(env_prefix="IBKR_MCP_", env_nested_delimiter="__")

    auth: AuthentikAuthConfig | None = Field(
        default=None, description="Authentik auth config. None means no front-door auth (local prototype only)."
    )

    gateway_base_url: str = Field(
        default="https://localhost:5000/v1/api",
        description=(
            "Base URL of the co-located Client Portal Gateway, including its API path prefix. "
            "The MCP server reaches this over the pod's localhost; it is never exposed publicly."
        ),
    )
    gateway_verify_tls: bool = Field(
        default=False,
        description=(
            "Verify the gateway's TLS certificate. The CP gateway serves a self-signed cert on "
            "localhost, so this defaults off for the in-pod hop; set true only if you front it "
            "with a trusted cert."
        ),
    )
    gateway_timeout: float = Field(default=30.0, description="Timeout (seconds) for gateway requests.")

    host: str = "0.0.0.0"
    port: int = 8765
    metrics_port: int = Field(
        default=9090, description="Prometheus metrics port (cluster-internal, not on the HTTPRoute)."
    )
    persistence: PersistenceConfig = Field(default=FilePersistence(), description="OAuth state storage backend.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert an optional YAML file source (below env) when ``IBKR_MCP_CONFIG_FILE`` is set."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get("IBKR_MCP_CONFIG_FILE"):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
