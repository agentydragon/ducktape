"""Authoring schema for a Go egress sidecar's shared platform gRPC listener."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PLATFORM_ROUTES_PORT = 3130
PLATFORM_ROUTES_LISTEN_ADDRESS = f"127.0.0.1:{PLATFORM_ROUTES_PORT}"


class PlatformRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_prefixes: tuple[str, ...] = Field(
        min_length=1, description="Exact gRPC service prefixes routed through this upstream and credential."
    )
    upstream: str = Field(min_length=1, description="host:port of the service, dialled in plaintext in-cluster.")
    token_file: Path | None = Field(
        default=None,
        description="Optional projected token file with the service's audience, mounted into the sidecar only.",
    )


class PlatformRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_address: str = Field(description="The single local listener for all direct platform gRPC routes.")
    routes: dict[str, PlatformRoute] = Field(
        default_factory=dict,
        description="Named service-prefix groups. Each keeps its own upstream connection and token audience.",
    )
