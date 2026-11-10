"""Authoring model for the Agentplane egress sidecar's Go process settings."""

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
RUNNER_TLS_MOUNT_PATH = "/run/agentplane/environment-runtime-tls"
# The workload CA alone, projected by the Sandbox Controller beside the runner's credential
# (`RunnerWorkloadTrustFile` in agentplane/sandbox_controller/environment_runtime_credential.go).
RUNNER_WORKLOAD_TRUST_FILE = Path(RUNNER_TLS_MOUNT_PATH) / "workload-ca.crt"


class SpiffeProxyAuth(BaseModel):
    """The workload SVID presented to the central proxy, the only hop authentication it accepts."""

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


class TLSProxyTransport(BaseModel):
    """mTLS to the central proxy, with explicit roots."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["tls"] = "tls"
    ca_file: Path
    auth: SpiffeProxyAuth

    @field_validator("ca_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("proxy transport ca_file must be an absolute path")
        return value


class Proxy(BaseModel):
    """Central proxy endpoint, hop authentication, and connection transport."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    address: str = Field(description="Central proxy host:port.")
    transport: TLSProxyTransport = Field(description="mTLS to the proxy, presenting the workload SVID.")

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


class WorkloadRoute(BaseModel):
    """One HTTP origin whose sidecar presents the Sandbox SVID to its destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream: str
    server_uri: str


class Settings(BaseModel):
    """The Go runtime reads the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE` and nothing
    else: no per-setting env variables and no flags."""

    model_config = ConfigDict(extra="forbid", strict=True)
    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

    proxy: Proxy = Field(description="The central egress proxy hop.")
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience. Destination tokens are independent "
        "of the proxy hop authentication.",
    )
    workload_routes: dict[str, WorkloadRoute] = Field(
        default_factory=dict,
        description="Exact HTTP origin to service-owned SVID TLS through an opaque central CONNECT.",
    )
    platform_routes: PlatformRoutes = Field(
        default_factory=lambda: PlatformRoutes(listen_address=PLATFORM_ROUTES_LISTEN_ADDRESS),
        description="One local gRPC listener dispatching allowlisted service prefixes to isolated upstream "
        "connections and their own route authentication.",
    )
    listen_address: str = Field(
        default="127.0.0.1:3128", description="Address the runner container's HTTP(S)_PROXY names."
    )
    readiness_listen_address: str = Field(default=READINESS_LISTEN_ADDRESS, description="HTTP address serving /readyz.")
