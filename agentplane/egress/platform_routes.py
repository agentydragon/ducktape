"""Authoring schema for a Go egress sidecar platform route."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlaintextRouteTransport(BaseModel):
    """A direct in-cluster gRPC hop, optionally authenticated with a route-specific bearer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["plaintext"] = "plaintext"
    token_file: Path | None = None


class TLSConnectRouteTransport(BaseModel):
    """TLS to the service through the central proxy's CONNECT tunnel, using the sidecar SVID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["tls_connect"] = "tls_connect"
    server_uri: str = Field(min_length=1, description="Exact SPIFFE identity the destination must present.")


PlatformRouteTransport = Annotated[PlaintextRouteTransport | TLSConnectRouteTransport, Field(discriminator="mode")]


class PlatformRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_address: str = Field(description="Address the workload dials for this service.")
    upstream: str = Field(min_length=1, description="Exact host:port of the service.")
    transport: PlatformRouteTransport = Field(description="How this route authenticates and reaches its service.")
