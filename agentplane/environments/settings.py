"""Authoring schema for the Go Sandbox Controller's deployment settings."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.identity.auth.listener import Listener
from agentplane.net.peertls.config import SANDBOX_CONTROLLER_URI, ClientTLS, Security, ServiceSPIFFEID
from agentplane.sandboxes.runtimeconfig.model_catalog import Catalog

type Account = Annotated[str, Field(pattern=r"^[^/]+/[^/]+$")]


class ServiceDelegator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spiffe_id: ServiceSPIFFEID
    connection_provenance: Literal[False] = False


class ServiceDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actors: tuple[ServiceDelegator, ...] = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=1)


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Account
    identity_callers: Literal[True]
    methods: tuple[str, ...] = Field(min_length=1)


class EnvironmentService(BaseModel):
    """Every-replica RPC settings, enabled only after the accepting image is pinned."""

    model_config = ConfigDict(extra="forbid")
    rpc: Listener
    policy_namespace: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    readers: tuple[Account, ...] = ()
    managers: tuple[Account, ...] = ()
    launchers: tuple[Account, ...] = ()
    operators: tuple[Account, ...] = ()
    service_readers: tuple[ServiceSPIFFEID, ...] = ()
    service_managers: tuple[ServiceSPIFFEID, ...] = ()
    service_launchers: tuple[ServiceSPIFFEID, ...] = ()
    service_operators: tuple[ServiceSPIFFEID, ...] = ()
    # Read-only automatic-wake eligibility must match the Sessions broker grants.
    wake_callers: tuple[Account, ...] = ()
    wake_service_callers: tuple[ServiceSPIFFEID, ...] = ()
    service_delegation: ServiceDelegation | None = None
    delegation: Delegation | None = None
    runtime_catalog: Catalog | None = None

    @model_validator(mode="after")
    def authentication(self) -> Self:
        if not any(method.mode == "spiffe" for method in self.rpc.auth):
            raise ValueError("EnvironmentService rpc.auth must include spiffe")
        for method in self.rpc.auth:
            if method.mode == "kubernetes_bearer" and method.audiences != {self.audience}:
                raise ValueError("EnvironmentService bearer audience must equal audience")
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
        if self.delegation is not None and any(
            not method.startswith("/agentplane.environments.v1.EnvironmentService/")
            or method.rsplit("/", 1)[1]
            not in {
                "ListEnvironments",
                "WatchEnvironments",
                "GetEnvironment",
                "WatchEnvironment",
                "ResumeEnvironment",
                "CreateEnvironment",
                "DeleteEnvironment",
                "ListEnvironmentTemplates",
                "GetEnvironmentIdentityRecord",
                "SuspendEnvironment",
                "SetEnvironmentWakeOnSend",
                "SetEnvironmentIdleSuspend",
                "SetEnvironmentIdentity",
            }
            for method in self.delegation.methods
        ):
            raise ValueError("delegation methods must belong to EnvironmentService")
        return self


class Settings(BaseModel):
    """Static deployment configuration read from the Controller's required YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE"
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
