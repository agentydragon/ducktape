"""Authoring model for the Agentplane egress sidecar's Go process settings."""

from pathlib import Path
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.egress.platform_routes import PlatformRoute
from agentplane.environment_runtime_transport.config import TLS

CONTAINER_NAME = "egress-sidecar"
RUNNER_TLS_MOUNT_PATH = "/run/agentplane/environment-runtime-tls"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129
READINESS_LISTEN_ADDRESS = f"{READINESS_HOST}:{READINESS_PORT}"
ProxyMode = Literal["token", "mtls"]


class ProxyTLSFiles(BaseModel):
    """The controller-issued Runner SVID and the cluster CA used by the proxy listener."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cert_file: Path
    key_file: Path
    ca_file: Path

    @model_validator(mode="after")
    def paths_are_absolute(self) -> Self:
        if any(not path.is_absolute() for path in (self.cert_file, self.key_file, self.ca_file)):
            raise ValueError("proxy TLS file paths must be absolute")
        return self


class WorkloadRoute(BaseModel):
    """One HTTP origin whose sidecar presents the Sandbox SVID to its destination."""

    model_config = ConfigDict(extra="forbid")
    upstream: str
    server_uri: str
    tls: TLS


class Settings(BaseModel):
    """The Go runtime reads the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE` and nothing
    else: no per-setting env variables and no flags."""

    model_config = ConfigDict(extra="forbid")
    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

    proxy_host: str = Field(description="Host of the central egress proxy every request is relayed to.")
    proxy_port: int = Field(default=8889, description="The central proxy's TLS-only listener port.")
    proxy_mode: ProxyMode = Field(
        description="Explicit hop mode. Token mode is retained for parked non-Sandbox callers, whose route is unavailable."
    )
    token_file: Path | None = Field(
        default=None, description="Token-mode-only credential for parked non-Sandbox sidecars. Never used with mTLS."
    )
    proxy_tls: ProxyTLSFiles | None = Field(
        default=None, description="Projected Runner SVID files, used only in mtls mode."
    )
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience.",
    )
    workload_routes: dict[str, WorkloadRoute] = Field(
        default_factory=dict, description="Exact HTTP host:port to service-owned SVID TLS via opaque central CONNECT."
    )
    platform_routes: dict[str, PlatformRoute] = Field(
        default_factory=dict,
        description="Agentplane services this Pod calls directly, by name: each gets a loopback gRPC "
        "listener that forwards with the service's token, bypassing the central proxy.",
    )
    listen_address: str = Field(
        default="127.0.0.1:3128", description="Address the runner container's HTTP(S)_PROXY names."
    )
    readiness_listen_address: str = Field(default=READINESS_LISTEN_ADDRESS, description="HTTP address serving /readyz.")

    @model_validator(mode="after")
    def proxy_mode_has_required_credentials(self) -> Self:
        if self.proxy_mode == "token":
            if self.proxy_tls is not None:
                raise ValueError("token mode forbids proxy_tls")
        else:
            if self.proxy_tls is None:
                raise ValueError("mtls mode requires proxy_tls")
            if self.token_file is not None:
                raise ValueError("mtls mode forbids token_file")
        if self.token_file is not None and not self.token_file.is_absolute():
            raise ValueError("token_file must be an absolute path")
        return self
