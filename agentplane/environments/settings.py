"""Authoring schema for the Go Sandbox Controller's deployment settings."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.net.peertls.config import SANDBOX_CONTROLLER_URI, ClientTLS, Security


class Settings(BaseModel):
    """Static deployment configuration read from the Controller's required YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    runner_gateway_tls: ClientTLS | None = None
    runner_security: Security

    namespace: str = Field(
        min_length=1, description="Namespace where the Sandboxes, their templates, Pods and state PVCs live."
    )
    metrics_bind_address: str = Field(default=":8080", min_length=1, description="Where Prometheus metrics are served.")
    health_probe_bind_address: str = Field(
        default=":8081", min_length=1, description="Where /healthz and /readyz are served."
    )
    runner_gateway_target: str = Field(
        min_length=1,
        description="Where the controller quiesces a Sandbox's runner through the stable Runner Gateway Service: "
        "one target, or a template such as `runner-gateway.{namespace}.svc:8080`.",
    )
    runner_gateway_token_file: Path | None = Field(
        default=None, description="Projected token path; absent when the controller presents its service SVID."
    )

    @model_validator(mode="after")
    def caller_authentication(self) -> Self:
        if self.runner_gateway_tls is not None:
            self.runner_gateway_tls.validate_caller(self.runner_gateway_token_file, SANDBOX_CONTROLLER_URI)
        elif self.runner_gateway_token_file is None:
            raise ValueError("a bearer token file or service SVID is required")
        return self
