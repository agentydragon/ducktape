"""Settings for service actors delegated to act on behalf of an Identity for named RPCs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplane.net.peertls.config import ServiceSPIFFEID


class ServiceDelegator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spiffe_id: ServiceSPIFFEID
    connection_provenance: Literal[False] = False


class ServiceDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actors: tuple[ServiceDelegator, ...] = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=1)
