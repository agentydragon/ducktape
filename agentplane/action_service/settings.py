"""Action Service configuration contract shared by deployment synthesis and runtime."""

from __future__ import annotations

from collections.abc import Set
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from agentplane.action_service.catalog import ActionGroup, Key
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.orchestration_v1.binding import OrchestrationExecutorBinding
from agentplane.action_service.push_settings import WebPushSettings
from agentplane.action_service.session_delegation.binding import SessionDelegationExecutorBinding
from agentplane.identity.auth.listener import Listener
from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    CENTRAL_SAMPLER_URI,
    SANDBOX_CONTROLLER_URI,
    ClientTLS,
    PolicyServicePeer,
    ServiceSPIFFEID,
)

DEFAULT_DIRECT_WAIT_SECONDS = 30.0


class ServicePeer(BaseModel):
    """A gRPC service this one calls as its workload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, description="gRPC host:port.")
    token_file: str = Field(default="", description="Projected token path; empty when the peer uses a service SVID.")
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    tls: ClientTLS | None = Field(
        default=None, description="Verify the peer SVID, optionally presenting the Action Service SVID."
    )

    @model_validator(mode="after")
    def caller_authentication(self) -> Self:
        if self.tls is not None:
            self.tls.validate_caller(self.token_file, ACTIONS_URI)
        elif not self.token_file:
            raise ValueError("a bearer token file or service SVID is required")
        return self


class EnvironmentServicePeer(ServicePeer):
    """Dedicated EnvironmentService endpoint on the Sandbox Controller."""

    @model_validator(mode="after")
    def controller_svid(self) -> Self:
        if self.tls is None or self.tls.server_uri != SANDBOX_CONTROLLER_URI:
            raise ValueError(f"environment_service.tls.server_uri must identify {SANDBOX_CONTROLLER_URI}")
        if not self.tls.uses_svid:
            raise ValueError("environment_service must present the Actions service SVID")
        return self


class Settings(BaseModel):
    """Complete non-secret deployment settings, read from a required YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_ACTIONS_CONFIG_FILE"
    database_url_env: ClassVar[str] = "AGENTPLANE_ACTIONS_DATABASE_URL"
    web_push_private_key_env: ClassVar[str] = "AGENTPLANE_ACTIONS_WEB_PUSH__PRIVATE_KEY_PEM"
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    rpc: Listener = Field(description="The single gRPC listener and its explicitly enabled authentication methods.")
    service_readers: list[ServiceSPIFFEID] = Field(
        exclude_if=lambda value: not value,
        default_factory=list,
        description="Exact service SPIFFE IDs granted read-only request access.",
    )
    operator_services: list[ServiceSPIFFEID] = Field(
        exclude_if=lambda value: not value,
        default_factory=list,
        description="Exact service SPIFFE IDs allowed to carry separately verified operator ID tokens.",
    )
    connection_delegation_service: ServiceSPIFFEID | Literal[""] = Field(
        exclude_if=lambda value: not value,
        default="",
        description="Exact MCP edge service SPIFFE ID allowed to delegate Connection operations.",
    )
    session_delegation_service: ServiceSPIFFEID | Literal[""] = Field(
        exclude_if=lambda value: not value,
        default="",
        description="Exact Central Sampler service SPIFFE ID allowed to delegate Session operations.",
    )
    policy_namespace: str = Field(
        description="Kubernetes namespace whose PolicySets and PolicyBindings this service watches; "
        "it writes `ActionsReady` conditions only on those bindings."
    )
    # CLEANUP(added 2026-11-26): require it and drop the Kubernetes Identity reads once the staging manifests set it
    #   (IDENTITY_READERS_BEHIND_POLICY_SERVICE).
    policy_service: PolicyServicePeer | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="PolicyService, the one source of every Identity this service reads; "
        "unset, it reads them from the API server in `policy_namespace`.",
    )
    caller_service_account_namespaces: Set[str] = Field(
        description="Kubernetes namespaces whose live WorkloadCredentials the SVID resolver reads; "
        "it does not grant Action approval."
    )
    direct_wait_seconds: float = Field(
        default=DEFAULT_DIRECT_WAIT_SECONDS,
        ge=0,
        allow_inf_nan=False,
        description="Maximum duration for orchestration_v1 Session operations.",
    )
    operator_oidc: OperatorOidcSettings | None = None
    action_groups: dict[Key, ActionGroup] = Field(
        default_factory=dict, description="Reviewed ActionGroup catalog, keyed by stable namespaced group key."
    )
    mcp_servers: dict[Key, McpOAuthServer] = Field(default_factory=dict)
    mcp_client_metadata: McpClientMetadataSettings | None = None
    web_push: WebPushSettings | None = Field(default=None, description="Optional Web Push delivery identity.")
    session_dispatcher: ServicePeer | None = Field(
        default=None, description="Where orchestration_v1 groups send Session commands; set exactly when one exists."
    )
    history_service: ServicePeer | None = Field(
        default=None, description="Where orchestration_v1 groups read Sessions; set exactly when one exists."
    )
    session_delegation_dispatcher: ServicePeer | None = Field(
        default=None,
        description="Delegated Discovery and Session Dispatcher endpoint/audience for session_delegation groups.",
    )
    environment_service: EnvironmentServicePeer | None = Field(
        default=None,
        description="Optional separate Sandbox Controller EnvironmentService endpoint; omission uses the configured Dispatcher peer.",
    )
    session_delegation_history: ServicePeer | None = Field(
        default=None, description="Delegated History endpoint/audience for session_delegation groups."
    )

    @field_serializer("caller_service_account_namespaces")
    def serialize_namespaces(self, namespaces: Set[str]) -> list[str]:
        return sorted(namespaces)

    @model_validator(mode="after")
    def validate_service_trust(self) -> Settings:
        if self.session_delegation_service and self.session_delegation_service != CENTRAL_SAMPLER_URI:
            raise ValueError(f"session_delegation_service must be {CENTRAL_SAMPLER_URI}")
        if not any(auth.mode == "spiffe" for auth in self.rpc.auth):
            raise ValueError("rpc must configure TLS and spiffe client trust")
        if self.policy_service is not None:
            self.policy_service.tls.validate_caller(None, ACTIONS_URI)
        return self

    @model_validator(mode="after")
    def shared_mcp_client_metadata(self) -> Settings:
        uses_shared_cimd = any(server.use_shared_cimd for server in self.mcp_servers.values())
        if uses_shared_cimd != (self.mcp_client_metadata is not None):
            raise ValueError("configure mcp_client_metadata exactly when an MCP server uses the shared CIMD")
        return self

    @model_validator(mode="after")
    def orchestration_peers(self) -> Settings:
        # Checks at synthesis what the service checks at startup (config.go validateOrchestration).
        orchestration = [
            group.executor
            for group in self.action_groups.values()
            if isinstance(group.executor, OrchestrationExecutorBinding)
        ]
        if any(binding.namespace != self.policy_namespace for binding in orchestration):
            raise ValueError(
                "an orchestration_v1 group launches in policy_namespace, where its launcher grants are read"
            )
        if (self.session_dispatcher is None, self.history_service is None) != (not orchestration, not orchestration):
            raise ValueError(
                "configure session_dispatcher and history_service exactly when an orchestration_v1 group calls them"
            )
        return self

    @model_validator(mode="after")
    def session_delegation_peers(self) -> Settings:
        delegated = any(
            isinstance(group.executor, SessionDelegationExecutorBinding) for group in self.action_groups.values()
        )
        peers = (self.session_delegation_dispatcher, self.session_delegation_history)
        if (peers[0] is None, peers[1] is None) != (not delegated, not delegated):
            raise ValueError(
                "configure session_delegation_dispatcher and session_delegation_history exactly when a "
                "session_delegation group is configured"
            )
        return self
