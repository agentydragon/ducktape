"""Authoring schema for the Go Sandbox Controller's deployment settings."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agentplane.environment_runtime_transport.config import ClientTLS


class Settings(BaseModel):
    """Static deployment configuration read from the Controller's required YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    runner_gateway_tls: ClientTLS | None = None

    namespace: str = Field(
        min_length=1, description="Namespace where the Sandboxes, their templates, Pods and state PVCs live."
    )
    metrics_bind_address: str = Field(default=":8080", min_length=1, description="Where Prometheus metrics are served.")
    health_probe_bind_address: str = Field(
        default=":8081", min_length=1, description="Where /healthz and /readyz are served."
    )
    runner_gateway_target: str = Field(
        min_length=1,
        description="Where the controller quiesces a Sandbox's runner through the Runner Gateway: one target, or a "
        "template of `{sandbox}` and `{namespace}` such as the Sandbox's own control Service.",
    )
    runner_gateway_token_file: Path = Field(
        description="Projected ServiceAccount token the Runner Gateway accepts from this controller."
    )
