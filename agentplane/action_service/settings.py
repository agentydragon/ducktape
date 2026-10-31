"""Action Service configuration contract shared by deployment synthesis and runtime."""

from __future__ import annotations

from collections.abc import Set
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.action_service.catalog import ActionGroup, Key
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.action_service.orchestration_v1.binding import OrchestrationExecutorBinding
from agentplane.action_service.push_settings import WebPushSettings
from agentplane.log_levels import LogLevels
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings

DEFAULT_DIRECT_WAIT_SECONDS = 30.0


class GitHubVisibilitySettings(BaseModel):
    """Go runtime settings for its unauthenticated GitHub repository visibility lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = "https://api.github.com"
    cache_ttl_seconds: float = Field(
        default=86400.0,
        ge=0,
        description="How long one repository's confirmed visibility is reused before GitHub is asked again.",
    )


class ServicePeer(BaseModel):
    """A gRPC service this one calls as its workload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, description="gRPC host:port.")
    token_file: str = Field(
        min_length=1, description="The rotating projected ServiceAccount token for the peer's audience."
    )
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)


class Settings(YamlFileSettings):
    """The service's configuration.

    Each field is a `--flag`, an `AGENTPLANE_ACTIONS_*` environment variable, and a key of the YAML
    file `AGENTPLANE_ACTIONS_CONFIG_FILE` names, in that order of precedence; the staging Deployment
    keeps the ActionGroup catalog in that file so backend/account changes need only a restart.
    """

    config_file_env: ClassVar[str] = "AGENTPLANE_ACTIONS_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_ACTIONS_",
        env_nested_delimiter="__",
        cli_parse_args=True,
        cli_kebab_case=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(description="Action Service-owned PostgreSQL database URL.")
    log_levels: LogLevels
    host: str = "127.0.0.1"
    port: int = 8080
    token_audience: str = Field(min_length=1, description="Accepted projected-token audience of Sandbox callers.")
    reader_accounts: frozenset[ServiceAccountRef] = frozenset()
    policy_namespace: str = Field(
        description="Kubernetes namespace whose PolicySets and PolicyBindings this service watches; "
        "it writes `ActionsReady` conditions only on those bindings."
    )
    caller_service_account_namespaces: Set[str] = Field(
        description="Kubernetes namespaces whose labeled ServiceAccounts may authenticate Action Service callers "
        "and whose ServiceAccounts this service watches; it does not grant Action approval."
    )
    direct_wait_seconds: float = Field(
        default=DEFAULT_DIRECT_WAIT_SECONDS,
        ge=0,
        allow_inf_nan=False,
        description="Maximum duration for orchestration_v1 Session operations.",
    )
    github_visibility: GitHubVisibilitySettings = Field(default_factory=GitHubVisibilitySettings)
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
