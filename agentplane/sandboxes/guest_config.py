"""Public guest configuration shared by VM generation and the test-only Python reference runner."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

CA_BUNDLE = Path("/run/agentplane/ca-certificates.crt")


class Endpoint(BaseModel):
    """A service the guest runner dials through `proxy_url`."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: str = Field(min_length=1, description="host:port to dial.")
    bearer: str = Field(min_length=1, description="Placeholder the egress proxy swaps for the guest's token.")


class GuestConfig(BaseModel):
    """Public per-environment launch values delivered on the ConfigMap disk."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    control: Endpoint
    ingress: Endpoint
    llm_base_url: str = Field(min_length=1)
    proxy_url: str = Field(min_length=1)
    platform_instructions: str = Field(min_length=1, description="Standing instructions for every Session.")
    console_autologin: bool = Field(
        default=False,
        description="Whether the serial console is a root shell without a login (vm.nix reads this field).",
    )

    @field_validator("llm_base_url", "proxy_url")
    @classmethod
    def http_origin(cls, value: str, info: ValidationInfo) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{info.field_name} must be an HTTP(S) origin URL")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError(f"{info.field_name} has an invalid port") from error
        if port == 0:
            raise ValueError(f"{info.field_name} must not use port 0")
        return value.rstrip("/")
