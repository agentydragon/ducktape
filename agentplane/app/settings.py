"""Agentplane app's deployment-authored settings file contract."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.oidc import OIDCSettings
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.app.sandbox_models import NewSandboxDefaults
from agentplane.log_levels import LogLevels
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    EGRESS_PROXY_URI,
    HISTORY_SERVICE_URI,
    MCP_EDGE_URI,
    NOTIFICATIONS_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ClientTLS,
)
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from util.urls import HttpEndpointUrl


class TraceSearchSettings(BaseModel):
    """Grafana Tempo Explore target for sandbox and session trace searches."""

    model_config = ConfigDict(extra="forbid")

    grafana_base_url: HttpEndpointUrl = Field(description="Grafana base URL, without query or fragment.")
    tempo_datasource_uid: str = Field(min_length=1, description="UID of the Grafana Tempo datasource.")
    deployment_environment: str = Field(
        min_length=1, description="Value of the spans' deployment environment resource."
    )


class LangfuseSearchSettings(BaseModel):
    """Langfuse project and metadata keys used to find one Sandbox's LLM traces."""

    model_config = ConfigDict(extra="forbid")

    base_url: HttpEndpointUrl = Field(description="Langfuse base URL, without query or fragment.")
    project_id: str = Field(min_length=1, description="Langfuse project ID containing the traces.")
    session_metadata_key: str = Field(
        default="attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id",
        min_length=1,
        description="Metadata key stamped for explicitly trusted central sampler Sessions.",
    )
    sandbox_namespace_metadata_key: str = Field(
        min_length=1, description="Langfuse trace metadata key holding the Sandbox namespace."
    )
    sandbox_service_account_metadata_key: str = Field(
        min_length=1, description="Langfuse trace metadata key holding the Sandbox ServiceAccount name."
    )


class FrontendConfig(BaseModel):
    """Browser-safe settings exposed by the generated AppService frontend-config route."""

    model_config = ConfigDict(extra="forbid")

    trace_search: TraceSearchSettings | None = Field(
        default=None, description="Optional Grafana Tempo trace links in sandbox and session menus."
    )
    langfuse_search: LangfuseSearchSettings | None = Field(
        default=None,
        description="Optional Langfuse links filtered by configured Sandbox namespace and ServiceAccount metadata.",
    )


class EnvironmentServicePeer(BaseModel):
    """Optional standalone EnvironmentService endpoint on the Sandbox Controller."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, description="Sandbox Controller gRPC host:port for EnvironmentService.")
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    tls: ClientTLS = Field(description="Verify the Sandbox Controller SVID and present the App service SVID.")

    @model_validator(mode="after")
    def validate_caller(self) -> EnvironmentServicePeer:
        if self.tls.server_uri != SANDBOX_CONTROLLER_URI:
            raise ValueError(f"environment_service.tls.server_uri must identify {SANDBOX_CONTROLLER_URI}")
        try:
            self.tls.validate_caller(None, APP_URI)
        except ValueError as exc:
            raise ValueError(f"environment_service caller authentication: {exc}") from exc
        return self


class Settings(BaseModel):
    """Complete non-secret runtime settings, authored in the mounted YAML file.

    Runtime database and OIDC credentials are supplied separately from Secrets. The service reads
    only `AGENTPLANE_CONFIG_FILE` plus those explicitly named credential inputs.
    """

    config_file_env: ClassVar[str] = "AGENTPLANE_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    models: ModelCatalog = Field(
        description="Deployment-authored model catalog: model choices and the harnesses that accept each model."
    )
    thread_presets: dict[str, ThreadPreset] = Field(
        default_factory=dict, description="App-owned ThreadPreset definitions keyed by stable name."
    )
    sandbox_presets: dict[str, SandboxPreset] = Field(
        default_factory=dict, description="App-owned Sandbox launch-form presets keyed by displayable name."
    )
    new_sandbox_defaults: NewSandboxDefaults = Field(
        default_factory=NewSandboxDefaults,
        description="Initial wake and idle suspension values of the new-Sandbox form, whatever the preset.",
    )
    egress_admin_url: HttpEndpointUrl = Field(
        description="The egress proxy's admin endpoint; its host and port serve gRPC."
    )
    connection_service_url: HttpEndpointUrl = Field(
        description="The Connection Service's generated HTTP/JSON facade; its host and port also serve gRPC health."
    )
    connection_service_tls: ClientTLS | None = Field(
        default=None, description="Optional verified TLS for MCP edge ConnectionService gRPC calls."
    )
    action_federation: ActionFederationSettings
    frontend_config: FrontendConfig = Field(
        default_factory=FrontendConfig,
        description="Browser-safe settings exposed by `GET /upstream/app-config/v1/frontend-config`.",
    )

    egress_admin_tls: ClientTLS | None = None
    runner_gateway_tls: ClientTLS | None = None
    session_dispatcher_tls: ClientTLS | None = None
    history_service_tls: ClientTLS | None = None
    notifications_tls: ClientTLS | None = None
    actions_tls: ClientTLS | None = None
    policy_service_tls: ClientTLS = Field(
        description="Verify the Policy Controller server SVID for PolicyService gRPC calls."
    )

    sandbox_namespace: str = Field(
        min_length=1,
        description="Namespace whose Sandbox inventory the app observes. Separate from the app's own namespace.",
    )
    runner_gateway_target: str = Field(
        min_length=1, description="The Runner Gateway's gRPC host:port, used by the health-status probe."
    )
    session_dispatcher_target: str = Field(
        min_length=1,
        description="Runner Gateway gRPC host:port for Session Dispatcher commands and app-brokered resume requests.",
    )
    environment_service: EnvironmentServicePeer | None = Field(
        default=None,
        description="Optional separate Sandbox Controller EnvironmentService endpoint; omission uses the Session Dispatcher peer.",
    )
    policy_service_target: str = Field(min_length=1)
    policy_service_token_file: Path | None = Path("/var/run/secrets/agentplane-policy/token")
    history_service_target: str = Field(min_length=1)
    notifications_url: HttpEndpointUrl = Field(
        description="The Notification Service's endpoint; its host and port serve the operator gRPC API."
    )
    notifications_token_file: Path | None = Path("/var/run/secrets/agentplane-notifications/token")
    notifications_request_timeout_s: float = Field(default=5, gt=0, allow_inf_nan=False)
    runner_gateway_token_file: Path | None = Field(
        default=Path("/var/run/secrets/agentplane-runner-gateway/token"),
        description="The rotating projected ServiceAccount token for the Runner Gateway audience.",
    )
    session_dispatcher_token_file: Path | None = Field(
        default=Path("/var/run/secrets/agentplane-session-dispatcher/token"),
        description="The rotating projected ServiceAccount token for the Session Dispatcher audience.",
    )
    session_dispatcher_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)
    history_service_token_file: Path | None = Path("/var/run/secrets/agentplane-history-service/token")
    history_service_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)

    log_levels: LogLevels = Field(default_factory=dict)
    listen_address: str = Field(
        default="127.0.0.1:8080", min_length=1, description="HTTP TCP bind address or inherited listening socket fd:N."
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    electric_url: HttpEndpointUrl | None = Field(
        default=None,
        description="Electric replicating the History Service fold; omit to disable authenticated thread-sync routes.",
    )
    egress_admin_token_file: Path | None = Path("/var/run/secrets/agentplane-egress-admin/token")
    egress_admin_timeout: float = Field(
        default=5, gt=0, allow_inf_nan=False, description="Seconds to wait for the proxy's initial decision snapshot."
    )
    shutdown_timeout: int = Field(
        default=5,
        ge=0,
        description="Seconds the HTTP server waits after SIGTERM for open requests and streams before cancelling "
        "them; the rest of the Deployment's grace period stops Session creation and closes the database.",
    )
    oidc: OIDCSettings | None = Field(
        default=None, description="Static OIDC relying-party settings; credentials arrive through Secret inputs."
    )

    @model_validator(mode="after")
    def validate_service_callers(self) -> Settings:
        """Keep service-token callers paired with their receiver and caller identities."""
        # Match Go's YAML loader: an omitted legacy default is cleared for SVID mode,
        # while an explicitly configured bearer token remains an authentication conflict.
        if (
            self.session_dispatcher_tls is not None
            and self.session_dispatcher_tls.uses_svid
            and "session_dispatcher_token_file" not in self.model_fields_set
        ):
            self.session_dispatcher_token_file = None
        if (
            self.runner_gateway_tls is not None
            and self.runner_gateway_tls.uses_svid
            and "runner_gateway_token_file" not in self.model_fields_set
        ):
            self.runner_gateway_token_file = None
        if (
            self.history_service_tls is not None
            and self.history_service_tls.uses_svid
            and "history_service_token_file" not in self.model_fields_set
        ):
            self.history_service_token_file = None
        if (
            self.policy_service_tls is not None
            and self.policy_service_tls.uses_svid
            and "policy_service_token_file" not in self.model_fields_set
        ):
            self.policy_service_token_file = None
        if (
            self.egress_admin_tls is not None
            and self.egress_admin_tls.uses_svid
            and "egress_admin_token_file" not in self.model_fields_set
        ):
            self.egress_admin_token_file = None
        if (
            self.notifications_tls is not None
            and self.notifications_tls.uses_svid
            and "notifications_token_file" not in self.model_fields_set
        ):
            self.notifications_token_file = None

        for name, tls, token_file, server_uris in (
            (
                "session_dispatcher",
                self.session_dispatcher_tls,
                self.session_dispatcher_token_file,
                (SESSION_DISPATCHER_URI,),
            ),
            (
                "runner_gateway",
                self.runner_gateway_tls,
                self.runner_gateway_token_file,
                (SESSION_DISPATCHER_URI, SANDBOX_CONTROLLER_URI),
            ),
            ("history_service", self.history_service_tls, self.history_service_token_file, (HISTORY_SERVICE_URI,)),
            ("policy_service", self.policy_service_tls, self.policy_service_token_file, (POLICY_CONTROLLER_URI,)),
            ("egress_admin", self.egress_admin_tls, self.egress_admin_token_file, (EGRESS_PROXY_URI,)),
            ("notifications", self.notifications_tls, self.notifications_token_file, (NOTIFICATIONS_URI,)),
        ):
            if tls is not None and tls.server_uri not in server_uris:
                raise ValueError(f"{name}_tls.server_uri must identify one of {server_uris}")
            try:
                if tls is not None:
                    tls.validate_caller(token_file, APP_URI)
                elif token_file is None:
                    raise ValueError("a token file is required when caller TLS is not configured")
            except ValueError as exc:
                raise ValueError(f"{name} caller authentication: {exc}") from exc

        # These facades carry a request-bound operator OAuth token. An App SVID may
        # identify the transport peer, but it does not replace that operator authority.
        for name, tls, server_uri in (
            ("actions", self.actions_tls, ACTIONS_URI),
            ("connection_service", self.connection_service_tls, MCP_EDGE_URI),
        ):
            if tls is None:
                continue
            if tls.server_uri != server_uri:
                raise ValueError(f"{name}_tls.server_uri must identify {server_uri}")
            if tls.uses_svid:
                try:
                    tls.validate_caller(None, APP_URI)
                except ValueError as exc:
                    raise ValueError(f"{name} caller authentication: {exc}") from exc
        return self
