"""Public guest configuration shared by VM generation and the test-only Python reference runner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationInfo, field_serializer, field_validator

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
    llm_base_url: AnyHttpUrl
    proxy_url: AnyHttpUrl
    platform_instructions: str = Field(min_length=1, description="Standing instructions for every Session.")
    console_autologin: bool = Field(
        default=False,
        description="Whether the serial console is a root shell without a login (vm.nix reads this field).",
    )
    harness_env: Mapping[str, str] | None = Field(
        default=None,
        description="Environment added to every harness process. The runner refuses a name the guest owns "
        "(PATH, HOME, the proxy and CA variables).",
    )

    @field_validator("llm_base_url", "proxy_url")
    @classmethod
    def http_origin(cls, value: AnyHttpUrl, info: ValidationInfo) -> AnyHttpUrl:
        if (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "/"}
            or value.query
            or value.fragment
            or value.port == 0
        ):
            raise ValueError(f"{info.field_name} must be an HTTP(S) origin URL")
        return value

    @field_serializer("llm_base_url", "proxy_url", when_used="json")
    def serialize_origin(self, value: AnyHttpUrl) -> str:
        return str(value).rstrip("/")
