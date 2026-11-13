"""Authoring schema for the Go Runner Gateway deployment settings.

The black-box tests serialize this model into the Go binary's strict settings loader.
"""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, field_validator, model_validator

from agentplane.environment_runtime_transport.config import ClientTLS, Security
from agentplane.identity.auth.listener import Listener
from agentplane.sandboxes.runtimeconfig.model_catalog import Catalog

type ServiceAccountName = Annotated[str, StringConstraints(pattern=r"^[^/]+/[^/]+$")]


class CentralSamplerTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1)
    token_file: Path


class DelegationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: ServiceAccountName = Field(description="Actions ServiceAccount trusted to delegate, as namespace/name.")
    identity_callers: Literal[True] = Field(
        description="Must be true: Actions may assert a live Identity in policy_namespace as the effective caller."
    )
    methods: tuple[str, ...] = Field(
        min_length=1, description="Exact full RPC method names served by the Runner Gateway that admit delegation."
    )


class Settings(BaseModel):
    """Complete non-secret static settings authored in the mounted YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
    dispatcher_database_url_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL"
    pod_name_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_POD_NAME"
    pod_ip_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_POD_IP"
    model_config = ConfigDict(extra="forbid")

    sandbox_namespace: str = Field(min_length=1, description="Namespace of Agentplane Sandboxes.")
    policy_namespace: str = Field(
        min_length=1, description="Namespace holding the Identities a workload SVID may name."
    )
    runner_token_file: Path = Field(description="Projected Gateway-audience token for cross-replica Runner calls.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    kubernetes_client_qps: float = Field(
        default=0.0,
        allow_inf_nan=False,
        description="Kubernetes client requests per second; zero uses client-go's default, -1 disables its limiter.",
    )

    rpc: Listener = Field(description="The shared TLS gRPC listener and its explicitly enabled auth modes.")
    gateway_tls: ClientTLS = Field(
        description="Verify the Session Dispatcher SVID on this Gateway's named control-Service calls."
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
    environment_readers: Set[ServiceAccountName] = Field(
        default_factory=frozenset, description="ServiceAccounts granted read access to all managed Environments."
    )
    environment_managers: Set[ServiceAccountName] = Field(
        default_factory=frozenset, description="Direct Pod-bound ServiceAccounts allowed to manage Environments."
    )
    environment_launchers: Set[ServiceAccountName] = Field(
        default_factory=frozenset, description="Direct Pod-bound Action brokers authorized to create Environments."
    )
    delegation: DelegationSettings | None = Field(
        default=None,
        description="Independent Actions delegation trust; does not grant direct or privileged caller access.",
    )
    gateway_callers: Set[ServiceAccountName] = Field(
        min_length=1,
        description="ServiceAccounts admitted to Gateway read-only Runtime diagnostics, as namespace/name.",
    )
    wake_callers: Set[ServiceAccountName] | None = Field(
        default=None,
        description=(
            "Dispatcher callers whose sends may wake a suspended Sandbox, as namespace/name; an empty list, "
            "like omission, grants no wake eligibility."
        ),
    )
    operator_callers: Set[ServiceAccountName] | None = Field(
        default=None,
        description=(
            "Direct, Pod-bound ServiceAccounts authorized to explicitly resume a Sandbox, including a requested stop; "
            "this does not grant delegation or automatic wake authority."
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

    @model_validator(mode="after")
    def validate_operator_callers(self) -> Settings:
        if self.operator_callers and not self.operator_callers <= self.dispatcher_callers:
            raise ValueError("operator_callers must be a subset of dispatcher_callers")
        return self

    @model_validator(mode="after")
    def rpc_audiences_are_method_scoped(self) -> Settings:
        audiences = {
            audience for method in self.rpc.auth if method.mode == "kubernetes_bearer" for audience in method.audiences
        }
        if self.dispatcher_audience == self.gateway_audience or audiences != {
            self.dispatcher_audience,
            self.gateway_audience,
        }:
            raise ValueError("rpc bearer audiences must be exactly dispatcher_audience and gateway_audience")
        if self.gateway_tls.server_uri != "spiffe://agentplane/session-dispatcher":
            raise ValueError("gateway_tls.server_uri must identify the Session Dispatcher service")
        return self

    @field_validator("kubernetes_client_qps")
    @classmethod
    def validate_kubernetes_client_qps(cls, value: float) -> float:
        if value < 0 and value != -1:
            raise ValueError("kubernetes_client_qps must be -1 or nonnegative")
        return value

    @field_serializer(
        "dispatcher_callers",
        "environment_readers",
        "environment_managers",
        "environment_launchers",
        "gateway_callers",
        "executor_callers",
    )
    def sorted_accounts(self, value: Set[str]) -> list[str]:
        return sorted(value, key=lambda account: tuple(account.split("/", maxsplit=1)))

    @field_serializer("wake_callers", "operator_callers")
    def sorted_optional_accounts(self, value: Set[str] | None) -> list[str] | None:
        return None if value is None else sorted(value, key=lambda account: tuple(account.split("/", maxsplit=1)))
