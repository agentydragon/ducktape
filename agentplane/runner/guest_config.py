"""Validate the read-only KubeVirt guest seed and start the runner service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from agentplane.runner.cgroups import AgentCgroups
from agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig
from agentplane.runner.main import async_main

CONFIG_PATH = Path("/run/agentplane/config.json")
STATE_DEVICE = Path("/dev/disk/by-id/virtio-state")
WORKSPACE_DEVICE = Path("/dev/disk/by-id/virtio-workspace")
AGENT_UID = 1000
AGENT_GID = 1000
RUNNER_STATE_DIR = Path("/state")
NATIVE_STATE_DIR = RUNNER_STATE_DIR / "native"
WORKSPACE_DIR = Path("/workspace")
CA_BUNDLE = Path("/run/agentplane/ca-certificates.crt")
KUBECONFIG = Path("/run/agentplane/kubeconfig")


class GuestConfig(BaseModel):
    """Public per-environment launch values delivered on the ConfigMap disk."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    environment_id: str = Field(min_length=1)
    listen: Literal["0.0.0.0:7000"]
    llm_base_url: str = Field(min_length=1)
    proxy_url: str = Field(min_length=1)
    model_context_windows: dict[str, int] = Field(default_factory=dict)
    format_blank_disks: list[Literal["state", "workspace"]] = Field(default_factory=list)

    @field_validator("environment_id")
    @classmethod
    def valid_environment_id(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("environment_id must not contain whitespace")
        return value

    @field_validator("model_context_windows")
    @classmethod
    def valid_context_windows(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not model or window <= 0 for model, window in value.items()):
            raise ValueError("model_context_windows must map non-empty model ids to positive integers")
        return value

    @field_validator("format_blank_disks")
    @classmethod
    def unique_format_authorizations(
        cls, value: list[Literal["state", "workspace"]]
    ) -> list[Literal["state", "workspace"]]:
        if len(value) != len(set(value)):
            raise ValueError("format_blank_disks must not contain duplicate disk names")
        return value

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


def read_guest_config(path: Path = CONFIG_PATH) -> GuestConfig:
    """Parse the boot config without echoing its contents to logs."""
    try:
        return GuestConfig.model_validate_json(path.read_bytes())
    except OSError as error:
        raise RuntimeError(f"cannot read guest config at {path}: {error}") from error


def make_runner_config(config: GuestConfig) -> RunnerConfig:
    """Build explicit runner and child configuration from the public seed."""
    llm_base_url = config.llm_base_url
    proxy_url = config.proxy_url
    environment = {
        "HOME": str(WORKSPACE_DIR / "home"),
        "USER": "runner",
        "LOGNAME": "runner",
        "PATH": "/run/current-system/sw/bin",
        "TZDIR": "/usr/share/zoneinfo",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "SSL_CERT_FILE": str(CA_BUNDLE),
        "NIX_SSL_CERT_FILE": str(CA_BUNDLE),
        "CURL_CA_BUNDLE": str(CA_BUNDLE),
        "GIT_SSL_CAINFO": str(CA_BUNDLE),
        "REQUESTS_CA_BUNDLE": str(CA_BUNDLE),
        "NODE_EXTRA_CA_CERTS": str(CA_BUNDLE),
        "PIP_CERT": str(CA_BUNDLE),
        "JAVA_TOOL_OPTIONS": (
            "-Djavax.net.ssl.trustStore=/run/agentplane/ca-certificates.p12 -Djavax.net.ssl.trustStoreType=PKCS12"
        ),
        "KUBECONFIG": str(KUBECONFIG),
    }
    return RunnerConfig(
        state_dir=RUNNER_STATE_DIR,
        environment=environment,
        model_context_windows=config.model_context_windows,
        claude=ClaudeLaunch(
            binary=Path("/run/current-system/sw/bin/claude"),
            base_url=llm_base_url,
            auth_token="agentplane-credential-agentplane-workload",
        ),
        codex=CodexLaunch(
            binary=Path("/run/current-system/sw/bin/codex"),
            base_url=f"{llm_base_url}/v1",
            api_key="agentplane-credential-agentplane-workload",
        ),
        process_isolation=AgentCgroups(
            root=Path("/sys/fs/cgroup/system.slice/agentplane-runner.service/workloads"),
            agent_uid=AGENT_UID,
            agent_gid=AGENT_GID,
            block_devices=(STATE_DEVICE, WORKSPACE_DEVICE),
        ),
        workspace_root=WORKSPACE_DIR,
        native_state_dir=NATIVE_STATE_DIR,
        initialization_cwd=WORKSPACE_DIR,
    )


def main() -> None:
    """Entry point used by the dedicated NixOS guest's systemd unit."""
    config = read_guest_config()
    asyncio.run(async_main(make_runner_config(config), config.listen))


def validate_main() -> None:
    """Validate the seed before storage is considered for initialization."""
    config = read_guest_config()
    for disk in config.format_blank_disks:
        print(disk)


if __name__ == "__main__":
    main()
