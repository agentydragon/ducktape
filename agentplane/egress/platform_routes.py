"""Authoring schema for a Go egress sidecar's shared platform gRPC listener."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PLATFORM_ROUTES_PORT = 3130
PLATFORM_ROUTES_LISTEN_ADDRESS = f"127.0.0.1:{PLATFORM_ROUTES_PORT}"


class RouteTransportMode(StrEnum):
    PLAINTEXT = "plaintext"
    TLS_CONNECT = "tls_connect"


class PlaintextRouteTransport(BaseModel):
    """A direct in-cluster gRPC hop, optionally authenticated with a route-specific bearer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal[RouteTransportMode.PLAINTEXT] = RouteTransportMode.PLAINTEXT
    token_file: Path | None = None


class TLSConnectRouteTransport(BaseModel):
    """TLS to the service through the central proxy's CONNECT tunnel, using the sidecar SVID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal[RouteTransportMode.TLS_CONNECT] = RouteTransportMode.TLS_CONNECT
    server_uri: str = Field(min_length=1, description="Exact expected service identity URI.")


PlatformRouteTransport = Annotated[PlaintextRouteTransport | TLSConnectRouteTransport, Field(discriminator="mode")]


class PlatformRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_prefixes: tuple[str, ...] = Field(
        min_length=1, description="Exact gRPC service prefixes routed through this upstream and transport."
    )
    upstream: str = Field(min_length=1, description="Exact host:port of the service.")
    transport: PlatformRouteTransport = Field(description="How this route authenticates and reaches its service.")


class PlatformRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_address: str = Field(description="The single local listener for all direct platform gRPC routes.")
    routes: dict[str, PlatformRoute] = Field(
        default_factory=dict, description="Named service-prefix groups sharing one local listener."
    )
