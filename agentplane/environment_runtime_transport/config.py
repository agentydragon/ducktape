"""Runner control security choices shared by the Runner and Gateway config files."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cert_file: str = Field(min_length=1)
    key_file: str = Field(min_length=1)
    ca_file: str = Field(min_length=1)

    @field_validator("cert_file", "key_file", "ca_file")
    @classmethod
    def absolute_file(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Runner control TLS paths must be absolute")
        return value


class MTLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["mtls"]
    tls: TLS


class Insecure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["insecure"]


Security = Annotated[MTLS | Insecure, Field(discriminator="mode")]
