"""Authoring model for the Agentplane egress sidecar's Go process settings."""

from enum import StrEnum
from ipaddress import AddressValueError, IPv6Address
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentplane.egress.platform_routes import PLATFORM_ROUTES_LISTEN_ADDRESS, PlatformRoutes

CONTAINER_NAME = "egress-sidecar"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129
READINESS_LISTEN_ADDRESS = f"{READINESS_HOST}:{READINESS_PORT}"


class ProxyAuthMode(StrEnum):
    KUBERNETES_BEARER = "kubernetes_bearer"


class ProxyTransportMode(StrEnum):
    PLAINTEXT = "plaintext"


class KubernetesBearerProxyAuth(BaseModel):
    """The projected bearer token used for the sidecar's central-proxy hop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal[ProxyAuthMode.KUBERNETES_BEARER] = ProxyAuthMode.KUBERNETES_BEARER
    token_file: Path

    @field_validator("token_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Kubernetes bearer token_file must be an absolute path")
        return value


class PlaintextProxyTransport(BaseModel):
    """The in-cluster plaintext connection to the central proxy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal[ProxyTransportMode.PLAINTEXT] = ProxyTransportMode.PLAINTEXT
    auth: KubernetesBearerProxyAuth


class Proxy(BaseModel):
    """The central proxy endpoint, hop authentication, and connection transport."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    address: str = Field(description="Central proxy host:port.")
    transport: PlaintextProxyTransport

    @field_validator("address")
    @classmethod
    def host_port(cls, value: str) -> str:
        if not value or any(character in value for character in "/\\?#@ \t\r\n"):
            raise ValueError("proxy address must be an exact host:port")
        if value.startswith("["):
            host, separator, port = value[1:].partition("]:")
            try:
                IPv6Address(host)
            except AddressValueError as error:
                raise ValueError("bracketed proxy address must contain an IPv6 host") from error
        else:
            host, separator, port = value.rpartition(":")
            if ":" in host:
                raise ValueError("IPv6 proxy addresses must use brackets")
        if not separator or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("proxy address must be an exact host:port")
        return value


class Settings(BaseModel):
    """The Go runtime reads the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE` and nothing
    else: no per-setting env variables and no flags."""

    model_config = ConfigDict(extra="forbid", strict=True)
    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

    proxy: Proxy = Field(description="The central egress proxy hop.")
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience. The hop token above is not one of "
        "these: it carries the proxy's own audience, which a destination validating its own refuses.",
    )
    platform_routes: PlatformRoutes = Field(
        default_factory=lambda: PlatformRoutes(listen_address=PLATFORM_ROUTES_LISTEN_ADDRESS),
        description="One local gRPC listener dispatching allowlisted service prefixes to isolated upstream "
        "connections and their own token audiences, bypassing the central proxy.",
    )
    listen_address: str = Field(
        default="127.0.0.1:3128", description="Address the runner container's HTTP(S)_PROXY names."
    )
    readiness_listen_address: str = Field(default=READINESS_LISTEN_ADDRESS, description="HTTP address serving /readyz.")
