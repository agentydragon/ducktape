"""Runner control security choices shared by the Runner and Gateway config files."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None

    @field_validator("cert_file", "key_file", "ca_file")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Runner control TLS paths must be absolute")
        return value

    @model_validator(mode="after")
    def one_source(self) -> Self:
        files = (self.cert_file, self.key_file, self.ca_file)
        if any(path is None for path in files):
            raise ValueError("cert_file, key_file, and ca_file are required")
        return self


class MTLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["mtls"] = "mtls"
    tls: TLS


class Insecure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["insecure"] = "insecure"


Security = Annotated[MTLS | Insecure, Field(discriminator="mode")]
