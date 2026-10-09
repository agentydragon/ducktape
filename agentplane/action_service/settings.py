"""Action Service configuration contract shared by deployment synthesis and runtime."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agentplane.action_service.catalog import ActionGroup, Key
from agentplane.action_service.github_policy.visibility import API_BASE_URL, CACHE_TTL_SECONDS
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.oauth_settings import OAuthSettings
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.action_service.push_settings import WebPushSettings
from agentplane.subjects import ServiceAccountRef

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
DEFAULT_MAX_WAIT_SECONDS = 30.0
DEFAULT_DIRECT_WAIT_SECONDS = 30.0
# Names the YAML settings file a deployment mounts; not a field, so not a flag.
CONFIG_FILE_ENV = "AGENTPLANE_ACTIONS_CONFIG_FILE"


def _validate_shared_mcp_client_metadata(
    mcp_servers: dict[Key, McpOAuthServer], client_metadata: McpClientMetadataSettings | None
) -> None:
    uses_shared_cimd = any(server.use_shared_cimd for server in mcp_servers.values())
    if uses_shared_cimd != (client_metadata is not None):
        raise ValueError("configure mcp_client_metadata exactly when an MCP server uses the shared CIMD")


class GitHubVisibilitySettings(BaseModel):
    """The unauthenticated GitHub REST lookup behind `github_public_repository` policies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = API_BASE_URL
    cache_ttl_seconds: float = Field(
        default=CACHE_TTL_SECONDS,
        ge=0,
        description="How long one repository's confirmed visibility is reused before GitHub is asked again.",
    )


class WebPushDeploymentSettings(BaseModel):
    """The Web Push fields cdk8s writes; the private key comes from the mounted Secret."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    public_base_url: str
    allowed_push_hosts: list[str]


class ActionServiceDeploymentSettings(BaseModel):
    """The Action Service settings authored by cdk8s and written to its YAML file.

    Runtime-only inputs such as the database URL and Web Push private key come from
    environment variables; `settings_file` validates those supplied leaves against the
    complete runtime `Settings` model.
    """

    model_config = ConfigDict(extra="forbid")

    operator_oidc: OperatorOidcSettings
    policy_namespace: str
    caller_service_account_namespaces: frozenset[str]

    @field_serializer("caller_service_account_namespaces", when_used="json")
    def serialize_caller_service_account_namespaces(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    direct_wait_seconds: float = Field(
        ge=0,
        allow_inf_nan=False,
        description="How long a direct tool call waits before returning its Action request ID.",
    )
    max_wait_seconds: float = Field(
        ge=0, allow_inf_nan=False, description="Maximum caller-requested wait on an Action receipt or result."
    )
    web_push: WebPushDeploymentSettings | None = None
    mcp_client_metadata: McpClientMetadataSettings | None = None
    mcp_servers: dict[Key, McpOAuthServer] = Field(default_factory=dict)
    action_groups: dict[Key, ActionGroup] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shared_mcp_client_metadata(self) -> ActionServiceDeploymentSettings:
        _validate_shared_mcp_client_metadata(self.mcp_servers, self.mcp_client_metadata)
        return self


class Settings(BaseSettings):
    """The service's configuration.

    Each field is a `--flag`, an `AGENTPLANE_ACTIONS_*` environment variable, and a key of the YAML
    file `AGENTPLANE_ACTIONS_CONFIG_FILE` names, in that order of precedence; the staging Deployment
    keeps the ActionGroup catalog in that file so backend/account changes need only a restart.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_ACTIONS_",
        env_nested_delimiter="__",
        cli_parse_args=True,
        cli_kebab_case=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(description="Action Service-owned PostgreSQL database URL.")
    host: str = "127.0.0.1"
    port: int = 8080
    token_audience: str = "agentplane-egress"
    reader_accounts: frozenset[ServiceAccountRef] = frozenset()
    policy_namespace: str = Field(
        default="agentplane-staging",
        description="Kubernetes namespace whose ActionPolicySets and ActionPolicyBindings this service watches; "
        "it writes Ready conditions only on those objects.",
    )
    caller_service_account_namespaces: frozenset[str] = Field(
        default=frozenset({"agentplane-staging"}),
        description="Kubernetes namespaces whose labeled ServiceAccounts may authenticate Action Service callers "
        "and whose ServiceAccounts this service watches; it does not grant Action approval.",
    )
    direct_wait_seconds: float = Field(
        default=DEFAULT_DIRECT_WAIT_SECONDS,
        ge=0,
        allow_inf_nan=False,
        description="How long a direct tool call waits before returning its Action request ID.",
    )
    policy_resync_seconds: int = Field(
        default=300, gt=0, description="Policy watch lifetime; every watched kind is relisted this often."
    )
    max_wait_seconds: float = Field(
        default=DEFAULT_MAX_WAIT_SECONDS,
        ge=0,
        allow_inf_nan=False,
        description="Maximum caller-requested wait on an Action receipt or result.",
    )
    github_visibility: GitHubVisibilitySettings = Field(default_factory=GitHubVisibilitySettings)
    operator_bearer_file: Path | None = None
    operator_oidc: OperatorOidcSettings | None = None
    oauth: OAuthSettings | None = None
    operator_subject: str = "configured-bff"
    action_groups: dict[Key, ActionGroup] = Field(
        default_factory=dict, description="Reviewed ActionGroup catalog, keyed by stable namespaced group key."
    )
    mcp_servers: dict[Key, McpOAuthServer] = Field(default_factory=dict)
    mcp_client_metadata: McpClientMetadataSettings | None = None
    web_push: WebPushSettings | None = Field(default=None, description="Optional Web Push delivery identity.")

    @model_validator(mode="after")
    def one_operator_authority(self) -> Settings:
        if self.operator_oidc is not None and self.operator_bearer_file is not None:
            raise ValueError("configure operator_oidc or legacy operator_bearer_file, never both")
        _validate_shared_mcp_client_metadata(self.mcp_servers, self.mcp_client_metadata)
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            # pydantic-settings silently ignores absent YAML files. An explicit deployment
            # binding must never turn into a healthy service with an empty catalog.
            if not Path(config_file).is_file():
                raise ValueError("configured Action Service settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
