"""Authoring schema for the Go Sessions (Session Dispatcher) deployment settings.

The black-box tests serialize this model into the Go binary's strict settings loader.
"""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from agentplane.identity.auth.listener import Listener
from agentplane.net.peertls.config import (
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ClientTLS,
    ServiceSPIFFEID,
)


class CentralSamplerTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1)
    token_file: Path | None = Field(
        default=None, description="Projected bearer token; omit when TLS uses the Sessions role service SVID."
    )
    tls: ClientTLS | None = Field(
        default=None,
        description="Verify the Central Sampler server SVID and optionally authenticate with a service SVID.",
    )

    @model_validator(mode="after")
    def validate_caller_authentication(self) -> CentralSamplerTarget:
        if self.tls is not None and self.tls.server_uri != CENTRAL_SAMPLER_URI:
            raise ValueError("central_sampler.tls.server_uri must identify the Central Sampler")
        if self.tls is not None:
            self.tls.validate_caller(self.token_file, GATEWAY_SESSIONS_URI)
        elif self.token_file is None:
            raise ValueError("a bearer token file or service SVID is required")
        return self


class ServiceDelegator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spiffe_id: ServiceSPIFFEID
    connection_provenance: Literal[False] = False


class ServiceDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actors: tuple[ServiceDelegator, ...] = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=1)


class Settings(BaseModel):
    """Complete non-secret static settings authored in the mounted YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_SESSIONS_CONFIG_FILE"
    dispatcher_database_url_env: ClassVar[str] = "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL"
    model_config = ConfigDict(extra="forbid")

    sandbox_namespace: str = Field(min_length=1, description="Namespace of Agentplane Sandboxes.")
    policy_namespace: str = Field(
        min_length=1, description="Namespace holding the Identities a workload SVID may name."
    )
    runner_token_file: Path | None = Field(
        default=None, description="Projected caller token; omit when gateway_tls uses the Sessions role SVID."
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    kubernetes_client_qps: float = Field(
        default=0.0,
        allow_inf_nan=False,
        description="Kubernetes client requests per second; zero uses client-go's default, -1 disables its limiter.",
    )

    rpc: Listener = Field(description="The shared TLS gRPC listener and its explicitly enabled auth modes.")
    gateway_tls: ClientTLS = Field(
        description="Verify the Environments SVID and authenticate with the Sessions role SVID."
    )
    runner_control_target_template: str = Field(
        default="environments.{namespace}.svc:8082",
        min_length=1,
        description="Stable Environments Service target or template used for each Sandbox Runtime proxy.",
    )
    dispatcher_service_delegation: ServiceDelegation | None = Field(default=None, exclude_if=lambda value: not value)
    dispatcher_service_callers: Set[ServiceSPIFFEID] = Field(
        default_factory=frozenset,
        exclude_if=lambda value: not value,
        description="Exact service SVIDs granted Dispatcher component authority.",
    )
    notice_acknowledgement_service_callers: Set[ServiceSPIFFEID] = Field(
        default_factory=frozenset,
        exclude_if=lambda value: not value,
        description="Direct Dispatcher service SVIDs explicitly authorized to acknowledge operator notices.",
    )
    wake_service_callers: Set[ServiceSPIFFEID] = Field(
        default_factory=frozenset,
        exclude_if=lambda value: not value,
        description="Exact Dispatcher service SVIDs allowed to request automatic Sandbox wake, subject to wakeOnSend.",
    )
    max_queued_per_session: int = Field(default=1000, ge=1, description="Queued commands per Session.")
    dispatcher_watch_lease_seconds: int = Field(default=300, ge=1, description="Dispatcher watch stream lease.")
    runner_request_timeout_seconds: int = Field(default=10, ge=1, description="Per-Sandbox Runner call timeout.")

    central_sampler: CentralSamplerTarget | None = Field(
        default=None, description="Central sampler target; omission disables central session admission."
    )

    @model_validator(mode="after")
    def validate_service_caller_scopes(self) -> Settings:
        if not self.wake_service_callers <= self.dispatcher_service_callers:
            raise ValueError("wake_service_callers must be a subset of dispatcher_service_callers")
        if not self.notice_acknowledgement_service_callers <= self.dispatcher_service_callers:
            raise ValueError("notice_acknowledgement_service_callers must be a subset of dispatcher_service_callers")
        if self.dispatcher_service_delegation is not None:
            dispatcher_methods = {
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeModel",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeReasoningEffort",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/InterruptTurn",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CreateSession",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetSession",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListSessions",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/WatchSessions",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetDispatch",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/WatchDispatches",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListDispatches",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListSessionDispatches",
                "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CancelDispatch",
            }
            if not set(self.dispatcher_service_delegation.methods) <= dispatcher_methods:
                raise ValueError("dispatcher_service_delegation methods must belong to SessionDispatcher")
        return self

    @model_validator(mode="after")
    def validate_receiver_and_control_tls(self) -> Settings:
        if self.gateway_tls.server_uri not in {SESSION_DISPATCHER_URI, SANDBOX_CONTROLLER_URI}:
            raise ValueError("gateway_tls.server_uri must identify the Gateway or Environments Runtime proxy")
        self.gateway_tls.validate_caller(self.runner_token_file, GATEWAY_SESSIONS_URI)
        return self

    @field_validator("kubernetes_client_qps")
    @classmethod
    def validate_kubernetes_client_qps(cls, value: float) -> float:
        if value < 0 and value != -1:
            raise ValueError("kubernetes_client_qps must be -1 or nonnegative")
        return value

    @field_serializer("wake_service_callers", "dispatcher_service_callers", "notice_acknowledgement_service_callers")
    def sorted_service_callers(self, value: Set[str]) -> list[str]:
        return sorted(value)
