"""Authentication allowlist and transport settings for a service listener."""

from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cert_file: Path
    key_file: Path

    @model_validator(mode="after")
    def absolute_paths(self) -> Self:
        if not self.cert_file.is_absolute() or not self.key_file.is_absolute():
            raise ValueError("tls requires absolute cert_file and key_file paths")
        return self


class SPIFFEAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["spiffe"] = "spiffe"
    ca_file: Path

    @model_validator(mode="after")
    def absolute_path(self) -> Self:
        if not self.ca_file.is_absolute():
            raise ValueError("spiffe requires an absolute ca_file")
        return self

    @model_serializer(mode="wrap")
    def serialize_discriminator(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        serialized["mode"] = self.mode
        return serialized


class KubernetesBearerAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["kubernetes_bearer"] = "kubernetes_bearer"
    audience: str = Field(min_length=1)

    @model_serializer(mode="wrap")
    def serialize_discriminator(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        serialized["mode"] = self.mode
        return serialized


type Authentication = Annotated[SPIFFEAuth | KubernetesBearerAuth, Field(discriminator="mode")]


class Listener(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen_address: str = Field(min_length=1)
    tls: TLS | None = None
    auth: list[Authentication] = Field(min_length=1)

    @model_validator(mode="after")
    def authentication_modes(self) -> Self:
        modes = [method.mode for method in self.auth]
        if len(set(modes)) != len(modes):
            raise ValueError("duplicate authentication mode")
        if "spiffe" in modes and self.tls is None:
            raise ValueError("spiffe requires tls")
        return self
