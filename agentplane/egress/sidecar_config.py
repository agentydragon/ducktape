"""Authoring model for the Agentplane egress sidecar's Go process settings."""

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agentplane.egress.platform_routes import PlatformRoute

CONTAINER_NAME = "egress-sidecar"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129
READINESS_LISTEN_ADDRESS = f"{READINESS_HOST}:{READINESS_PORT}"


class Settings(BaseModel):
    """The Go runtime reads the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE` and nothing
    else: no per-setting env variables and no flags."""

    model_config = ConfigDict(extra="forbid")
    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

    proxy_host: str = Field(description="Host of the central egress proxy every request is relayed to.")
    proxy_port: int = Field(default=8888, description="The central proxy's listener port.")
    token_file: Path = Field(description="The projected ServiceAccount token file, re-read on every request.")
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience. The hop token above is not one of "
        "these: it carries the proxy's own audience, which a destination validating its own refuses.",
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
