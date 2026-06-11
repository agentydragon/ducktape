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
`MCP_FACADE_UPSTREAM__KIND=stdio`).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.persistence import FilePersistence, PersistenceConfig


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


class FacadeSettings(BaseSettings):
    """Config for the MCP OAuth facade."""

    model_config = SettingsConfigDict(env_prefix="MCP_FACADE_", env_nested_delimiter="__")

    auth: AuthentikAuthConfig
    upstream: Upstream
    facade_name: str = Field(description="Human-readable name shown in MCP server metadata.")
    instructions: str | None = None
    host: str = "0.0.0.0"
    port: int = 8765
    # Prometheus metrics listen on a separate port so they are scraped
    # cluster-internally and never exposed through the facade's public
    # HTTPRoute (which forwards every path on `port` to the internet).
    metrics_port: int = 9090
    persistence: PersistenceConfig = FilePersistence()

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
