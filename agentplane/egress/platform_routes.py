"""Authoring schema for a Go egress sidecar platform route."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PlatformRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_address: str = Field(description="Address the workload dials for this service.")
    upstream: str = Field(min_length=1, description="host:port of the service, dialled in plaintext in-cluster.")
    token_file: Path | None = Field(
        default=None,
        description="Optional projected token file with the service's audience, mounted into the sidecar only.",
    )
