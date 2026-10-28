"""Authoring schema for the Go Sandbox Controller's deployment settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Fields are `AGENTPLANE_SANDBOX_CONTROLLER_*` environment variables. Keep them in sync with the keys of
    `cmd/config.go`."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_SANDBOX_CONTROLLER_")

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
    runner_certificate_issuer: str = Field(
        min_length=1,
        description="Namespace-scoped cert-manager Issuer that signs immutable runner workload credentials.",
    )
