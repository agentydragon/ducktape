"""Authoring model for the Agentplane egress sidecar's Go process settings."""

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.egress.platform_routes import PlatformRoute
from util.yaml_file_settings import YamlFileSettings

CONTAINER_NAME = "egress-sidecar"
REFUSED_HEADER = "x-agentplane-egress-sidecar"
PROJECTED_TOKEN_HEADER = "X-Agentplane-Workload-Token"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129


class Settings(YamlFileSettings):
    """The Go runtime reads the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, then the
    matching `AGENTPLANE_EGRESS_SIDECAR_*` values, then the `--flags`, each over the one before."""

    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_EGRESS_SIDECAR_", cli_parse_args=True, cli_kebab_case=True)

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
    listen_host: str = Field(default="127.0.0.1", description="Bind address; loopback, the runner's own Pod.")
    listen_port: int = Field(default=3128, description="Port the runner container's HTTP(S)_PROXY names.")
    readiness_host: str = Field(default=READINESS_HOST, description="Bind address for the kubelet readiness endpoint.")
    readiness_port: int = Field(default=READINESS_PORT, description="HTTP port serving /readyz.")
