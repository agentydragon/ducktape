"""Authoring schema for the Go Runner Gateway deployment settings.

The black-box tests serialize this model into the Go binary's strict settings loader.
"""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from pydantic_settings import SettingsConfigDict

from agentplane.environment_runtime_transport.config import Security
from agentplane.sandboxes.runtimeconfig.model_catalog import Catalog
from util.yaml_file_settings import YamlFileSettings

type ServiceAccountName = Annotated[str, StringConstraints(pattern=r"^[^/]+/[^/]+$")]


class CentralSamplerTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1)
    token_file: Path


class DelegationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: ServiceAccountName = Field(description="Actions ServiceAccount trusted to delegate, as namespace/name.")
    caller_namespaces: tuple[str, ...] = Field(
        min_length=1, description="Namespaces whose caller identity Actions may assert."
    )
    methods: tuple[str, ...] = Field(
        min_length=1, description="Exact full RPC method names served by the Runner Gateway that admit delegation."
    )


class Settings(YamlFileSettings):
    """Fields are YAML keys; each also accepts an `AGENTPLANE_RUNNER_GATEWAY_*` environment override.
    Deployments write the caller lists to the file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_RUNNER_GATEWAY_")

    sandbox_namespace: str = Field(min_length=1, description="Namespace of Agentplane Sandboxes.")
    dispatcher_database_url: SecretStr = Field(description="The Dispatcher queue's PostgreSQL URL.")
    pod_name: str | None = Field(
        default=None,
        min_length=1,
        description="Name this replica holds Sandbox connection leases under; the hostname when unset.",
    )
    pod_ip: str = Field(min_length=1, description="Address advertised in each Sandbox control EndpointSlice.")
    runner_token_file: Path = Field(description="Projected Gateway-audience token for cross-replica Runner calls.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    kubernetes_client_qps: float = Field(
        default=0.0,
        allow_inf_nan=False,
        description="Kubernetes client requests per second; zero uses client-go's default, -1 disables its limiter.",
    )

    listen_address: str = Field(
        default=":8080", min_length=1, description="gRPC listen address for authenticated platform callers."
    )
    http_listen_address: str | None = Field(
        default=None,
        min_length=1,
        description="HTTP/JSON listen address of the Dispatcher's generated facade; unset serves none.",
    )
    proxy_port: int = Field(
        default=8080, ge=1, le=65535, description="Port advertised in each Sandbox control EndpointSlice."
    )
    runner_control_target_template: str = Field(
        default="{sandbox}-control.{namespace}.svc:8080",
        min_length=1,
        description="Target or target template the Dispatcher uses for each Sandbox runner proxy.",
    )
    dispatcher_audience: str = Field(min_length=1, description="Dispatcher caller TokenReview audience.")
    gateway_audience: str = Field(min_length=1, description="Gateway caller TokenReview audience.")
    runner_security: Security
    dispatcher_callers: Set[ServiceAccountName] = Field(
        min_length=1, description="ServiceAccounts admitted as Dispatcher callers, as namespace/name."
    )
    delegation: DelegationSettings | None = Field(
        default=None,
        description="Independent Actions delegation trust; does not grant direct or privileged caller access.",
    )
    gateway_callers: Set[ServiceAccountName] = Field(
        min_length=1, description="ServiceAccounts admitted to the Gateway's Runner proxy, as namespace/name."
    )
    wake_callers: Set[ServiceAccountName] | None = Field(
        default=None,
        description=(
            "Dispatcher callers whose sends may wake a suspended Sandbox, as namespace/name; an empty list, "
            "like omission, grants no wake eligibility."
        ),
    )
    sandbox_controller_account: ServiceAccountName | None = Field(
        default=None, description="ServiceAccount admitted to Quiesce through the Gateway, as namespace/name."
    )
    max_queued_per_session: int = Field(default=1000, ge=1, description="Queued commands per Session.")
    dispatcher_watch_lease_seconds: int = Field(default=300, ge=1, description="WatchDispatches stream lease.")
    runner_request_timeout_seconds: int = Field(default=10, ge=1, description="Per-Sandbox Runner call timeout.")

    central_sampler: CentralSamplerTarget | None = Field(
        default=None, description="Central sampler target; omission disables central session admission."
    )
    executor_callers: Set[ServiceAccountName] = Field(
        default_factory=frozenset, description="ServiceAccounts allowed to open fixed exec-server streams."
    )
    runtime_catalog: Catalog | None = Field(
        default=None,
        description="Deployment-published runtime offers and template-scoped workspace guidance for discovery.",
    )

    @field_validator("kubernetes_client_qps")
    @classmethod
    def validate_kubernetes_client_qps(cls, value: float) -> float:
        if value < 0 and value != -1:
            raise ValueError("kubernetes_client_qps must be -1 or nonnegative")
        return value
