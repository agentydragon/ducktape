"""Runner control security choices shared by the Runner and Gateway config files."""

from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    workload_api_socket: str | None = None

    @field_validator("cert_file", "key_file", "ca_file")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Runner control TLS paths must be absolute")
        return value

    @field_validator("workload_api_socket")
    @classmethod
    def unix_socket_uri(cls, value: str | None) -> str | None:
        if value is None:
            return value
        endpoint = urlsplit(value)
        if (
            endpoint.scheme != "unix"
            or endpoint.netloc
            or not Path(endpoint.path).is_absolute()
            or endpoint.query
            or endpoint.fragment
            or value != f"unix://{endpoint.path}"
        ):
            raise ValueError("workload_api_socket must be a unix:///absolute/path URI")
        return value

    @model_validator(mode="after")
    def one_source(self) -> Self:
        files = (self.cert_file, self.key_file, self.ca_file)
        if self.workload_api_socket is not None:
            if any(path is not None for path in files):
                raise ValueError("workload_api_socket cannot be combined with cert_file, key_file, or ca_file")
        elif any(path is None for path in files):
            raise ValueError("cert_file, key_file, and ca_file are all required without workload_api_socket")
        return self


class MTLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["mtls"] = "mtls"
    tls: TLS


class Insecure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["insecure"] = "insecure"


Security = Annotated[MTLS | Insecure, Field(discriminator="mode")]
