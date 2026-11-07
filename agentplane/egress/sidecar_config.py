"""Authoring model for the Agentplane egress sidecar's Go process settings."""

import ipaddress
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentplane.egress.platform_routes import PlatformRoute

CONTAINER_NAME = "egress-sidecar"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129
READINESS_LISTEN_ADDRESS = f"{READINESS_HOST}:{READINESS_PORT}"
RUNNER_TLS_MOUNT_PATH = "/run/agentplane/environment-runtime-tls"


class SpiffeProxyAuth(BaseModel):
    """The Runtime SVID presented to the central proxy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["spiffe"] = "spiffe"
    cert_file: Path
    key_file: Path

    @field_validator("cert_file", "key_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("SPIFFE proxy authentication paths must be absolute")
        return value


class KubernetesBearerProxyAuth(BaseModel):
    """The sidecar-only projected bearer used to authenticate the central proxy hop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["kubernetes_bearer"] = "kubernetes_bearer"
    token_file: Path

    @field_validator("token_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Kubernetes bearer token_file must be an absolute path")
        return value


ProxyAuth = Annotated[SpiffeProxyAuth | KubernetesBearerProxyAuth, Field(discriminator="mode")]


class PlaintextProxyTransport(BaseModel):
    """The legacy in-cluster plaintext connection to the central proxy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["plaintext"] = "plaintext"
    auth: KubernetesBearerProxyAuth


class TLSProxyTransport(BaseModel):
    """TLS to the central proxy, with explicit roots and tagged hop authentication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["tls"] = "tls"
    ca_file: Path
    auth: ProxyAuth

    @field_validator("ca_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("proxy transport ca_file must be an absolute path")
        return value


ProxyTransport = Annotated[PlaintextProxyTransport | TLSProxyTransport, Field(discriminator="mode")]


class Proxy(BaseModel):
    """Central proxy endpoint, hop authentication, and connection transport."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    address: str = Field(description="Central proxy host:port.")
    transport: ProxyTransport = Field(description="Plaintext or server-authenticated TLS to the proxy.")

    @field_validator("address")
    @classmethod
    def host_port(cls, value: str) -> str:
        if not value or any(character in value for character in "/\\?#@ \t\r\n"):
            raise ValueError("proxy address must be an exact host:port")
        if value.startswith("["):
            host, separator, port = value[1:].partition("]:")
            try:
                ipaddress.IPv6Address(host)
            except ipaddress.AddressValueError as error:
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

    model_config = ConfigDict(extra="forbid")
    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

    proxy: Proxy = Field(description="The central egress proxy hop.")
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience. Destination tokens are independent "
        "of the proxy hop authentication.",
    )
    platform_routes: dict[str, PlatformRoute] = Field(
        default_factory=dict,
        description="Agentplane services this Pod calls by name. Each gets a local gRPC listener; "
        "the route transport chooses a direct bearer hop or SVID-authenticated TLS through the central proxy.",
    )
    listen_address: str = Field(
        default="127.0.0.1:3128", description="Address the runner container's HTTP(S)_PROXY names."
    )
    readiness_listen_address: str = Field(default=READINESS_LISTEN_ADDRESS, description="HTTP address serving /readyz.")

    @model_validator(mode="after")
    def tls_routes_require_spiffe_proxy_auth(self) -> Settings:
        has_tls_route = any(route.transport.mode == "tls_connect" for route in self.platform_routes.values())
        if has_tls_route and not (
            isinstance(self.proxy.transport, TLSProxyTransport)
            and isinstance(self.proxy.transport.auth, SpiffeProxyAuth)
        ):
            raise ValueError("TLS CONNECT platform routes require TLS proxy transport with SPIFFE authentication")
        return self
