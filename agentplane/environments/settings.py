"""Authoring schema for the Go Sandbox Controller's deployment settings."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.identity.auth.listener import Listener
from agentplane.net.peertls.config import SANDBOX_CONTROLLER_URI, ClientTLS, Security, ServiceSPIFFEID
from agentplane.sandboxes.runtimeconfig.model_catalog import Catalog


class ServiceDelegator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spiffe_id: ServiceSPIFFEID
    connection_provenance: Literal[False] = False


class ServiceDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actors: tuple[ServiceDelegator, ...] = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=1)


class RuntimeProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner_security: Security
    readers: tuple[ServiceSPIFFEID, ...] = ()
    session_callers: tuple[ServiceSPIFFEID, ...] = ()
    controller_callers: tuple[ServiceSPIFFEID, ...] = ()
    executor_callers: tuple[ServiceSPIFFEID, ...] = ()

    @model_validator(mode="after")
    def file_credentials(self) -> Self:
        if self.runner_security.mode == "mtls" and self.runner_security.tls.workload_api_socket is not None:
            raise ValueError("Runtime proxy runner_security requires file-backed client credentials")
        return self


class EnvironmentService(BaseModel):
    """Every-replica RPC settings, enabled only after the accepting image is pinned."""

    model_config = ConfigDict(extra="forbid")
    rpc: Listener
    policy_namespace: str = Field(min_length=1)
    service_readers: tuple[ServiceSPIFFEID, ...] = ()
    service_managers: tuple[ServiceSPIFFEID, ...] = ()
    service_launchers: tuple[ServiceSPIFFEID, ...] = ()
    service_operators: tuple[ServiceSPIFFEID, ...] = ()
    # Read-only automatic-wake eligibility must match the Sessions broker grants.
    wake_service_callers: tuple[ServiceSPIFFEID, ...] = ()
    service_delegation: ServiceDelegation | None = None
    runtime_proxy: RuntimeProxy | None = Field(default=None, exclude_if=lambda value: value is None)
    runtime_catalog: Catalog | None = None

    @model_validator(mode="after")
    def authentication(self) -> Self:
        if not any(method.mode == "spiffe" for method in self.rpc.auth):
            raise ValueError("EnvironmentService rpc.auth must include spiffe")
        if self.service_delegation is not None:
            allowed = {
                "/agentplane.environments.v1.EnvironmentService/ListEnvironments",
                "/agentplane.environments.v1.EnvironmentService/WatchEnvironments",
                "/agentplane.environments.v1.EnvironmentService/GetEnvironment",
                "/agentplane.environments.v1.EnvironmentService/WatchEnvironment",
                "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",
            }
            if not set(self.service_delegation.methods) <= allowed:
                raise ValueError("service_delegation methods must be delegatable EnvironmentService methods")
        return self


class Settings(BaseModel):
    """Static deployment configuration read from the Controller's required YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_ENVIRONMENTS_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    runner_gateway_tls: ClientTLS | None = None
    runner_security: Security
    environment_service: EnvironmentService | None = Field(default=None, exclude_if=lambda value: value is None)

    namespace: str = Field(
        min_length=1, description="Namespace where the Sandboxes, their templates, Pods and state PVCs live."
    )
    metrics_bind_address: str = Field(default=":8080", min_length=1, description="Where Prometheus metrics are served.")
    health_probe_bind_address: str = Field(
        default=":8081", min_length=1, description="Where /healthz and /readyz are served."
    )
    runner_gateway_target: str = Field(
        min_length=1,
        description="Where the controller quiesces a Sandbox's Runtime through the Environments proxy Service: "
        "one target, or a template such as `environments.{namespace}.svc:8082`.",
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
