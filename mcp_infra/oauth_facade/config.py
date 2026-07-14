"""Settings for a generic Authentik-backed MCP OAuth facade.

The facade fronts an upstream MCP server with an Authentik OAuth2 gate. The
upstream can be either:

- HTTP: a Streamable HTTP MCP server (e.g. an internal service reachable via
  bearer-authenticated cluster DNS). The Tana facade is the canonical example.
- Stdio: a subprocess that speaks MCP over stdin/stdout (e.g. a vendored
  Node/Python CLI). The subprocess inherits the facade pod's environment, so
  Secret-mounted env vars (like `MANIFOLD_API_KEY`) reach the child unchanged.

All fields load from `MCP_FACADE_*` env vars; nested fields use `__` as the
delimiter (e.g. `MCP_FACADE_AUTH__OIDC_ISSUER`,
`MCP_FACADE_UPSTREAM__KIND=stdio`). Non-secret structured config may instead live
in a YAML file: point `MCP_FACADE_CONFIG_FILE` at it and put e.g. `facade_name`
and the `tools` allowlist there as real YAML (a clean list, not a JSON-in-env
string). Env still outranks the file, and secrets and `upstream` stay in env —
keep the YAML and env fields disjoint so no single nested field is split across
sources.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.persistence import FilePersistence, PersistenceConfig
from mcp_infra.tool_filter import ToolFilter


class HttpUpstream(BaseModel):
    """Upstream MCP server reachable over Streamable HTTP."""

    kind: Literal["http"] = "http"
    url: str = Field(description="URL of the upstream Streamable HTTP MCP endpoint.")
    bearer_token: str | None = Field(
        default=None, description="Optional server-held bearer token forwarded to the upstream."
    )


class StdioUpstream(BaseModel):
    """Upstream MCP server spawned as a stdio subprocess."""

    kind: Literal["stdio"] = "stdio"
    command: list[str] = Field(
        description=("argv for the upstream subprocess. Element 0 is the executable; the rest are its args.")
    )


Upstream = Annotated[HttpUpstream | StdioUpstream, Field(discriminator="kind")]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class FacadeLoggingConfig(BaseModel):
    """Optional facade observability knobs."""

    mcp_messages: bool = Field(
        default=False, description="Install FastMCP structured middleware that logs MCP protocol messages."
    )
    mcp_message_level: LogLevel = Field(default="INFO", description="Log level for MCP protocol message logs.")
    mcp_payloads: bool = Field(default=False, description="Whether to include full MCP payloads in protocol logs.")
    mcp_payload_length: bool = Field(
        default=True, description="Whether MCP protocol logs include serialized payload lengths."
    )
    mcp_methods: list[str] | None = Field(
        default=None, description="Optional MCP method allowlist for protocol message logs; default logs every method."
    )


class StaticBearerClientAuth(BaseModel):
    """Cluster-internal client auth: every MCP request must carry a fixed bearer.

    Alternative to the public Authentik OAuth gate (`auth`) for facades that are
    not publicly routed. The token is a shared secret, distinct from
    `upstream.bearer_token` (which the facade sends to the upstream); here the
    network boundary plus this secret are the access control. Callers send
    `Authorization: Bearer <static_bearer>`; probes (/healthz, /readyz) bypass it.
    """

    static_bearer: str = Field(description="Fixed bearer token required on every MCP request.")


class FacadeSettings(BaseSettings):
    """Config for the MCP OAuth facade.

    Exactly one client-auth mode is required: `auth` (public Authentik OAuth) or
    `client_auth` (cluster-internal static bearer).
    """

    model_config = SettingsConfigDict(env_prefix="MCP_FACADE_", env_nested_delimiter="__")

    auth: AuthentikAuthConfig | None = None
    client_auth: StaticBearerClientAuth | None = None
    upstream: Upstream
    tools: ToolFilter | None = Field(
        default=None, description="Optional allow/deny filter over the tools exposed to callers (default: expose all)."
    )
    facade_name: str = Field(description="Human-readable name shown in MCP server metadata.")
    instructions: str | None = None
    host: str = "0.0.0.0"
    port: int = 8765
    # Prometheus metrics listen on a separate port so they are scraped
    # cluster-internally and never exposed through the facade's public
    # HTTPRoute (which forwards every path on `port` to the internet).
    metrics_port: int = 9090
    persistence: PersistenceConfig = FilePersistence()
    logging: FacadeLoggingConfig = Field(default_factory=FacadeLoggingConfig)

    probe_interval_seconds: float = Field(
        default=60.0, description="How often the background probe lists upstream tools to refresh /metrics and /readyz."
    )
    probe_max_staleness_seconds: float = Field(
        default=195.0,
        description=(
            "Readiness staleness window. /readyz reports ready only if a probe succeeded with >0 tools within "
            "this many seconds. Default > 3 probe intervals so a single transient probe failure does not flap "
            "readiness; sustained upstream failure flips the pod NotReady."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_client_auth(self) -> FacadeSettings:
        if (self.auth is None) == (self.client_auth is None):
            raise ValueError("set exactly one of `auth` (public Authentik OAuth) or `client_auth` (static bearer)")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert an optional YAML file source (below env) when `MCP_FACADE_CONFIG_FILE` is set."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get("MCP_FACADE_CONFIG_FILE"):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
