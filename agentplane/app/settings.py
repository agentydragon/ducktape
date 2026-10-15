"""Agentplane app settings and deployment-authored configuration contract."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import ClassVar

from pydantic import Field, field_serializer
from pydantic_settings import SettingsConfigDict

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.log_levels import LogLevels
from util.urls import HttpEndpointUrl
from util.yaml_file_settings import YamlFileSettings


class AppSettingsConfig(YamlFileSettings):
    """Deployment-authored app settings, before runtime-only inputs arrive.

    The runtime `Settings` model extends this, so cdk8s can construct this typed fragment
    without fabricating the app's namespace or database URL.
    """

    config_file_env: ClassVar[str] = "AGENTPLANE_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_", cli_parse_args=True, cli_kebab_case=True)

    models: ModelCatalog = Field(
        description="Every model agentplane can open a session with and which harnesses accept it, as JSON: "
        '{"models": [{"model": "...", "display_name": "...", "reasoning_efforts": [...]}], '
        '"harnesses": {"HARNESS_CLAUDE": ["..."], "HARNESS_CODEX": ["..."]}}.'
    )
    thread_presets: dict[str, ThreadPreset] = Field(
        default_factory=dict, description="App-owned ThreadPreset definitions keyed by stable name."
    )
    sandbox_presets: dict[str, SandboxPreset] = Field(
        default_factory=dict, description="App-owned Sandbox launch-form presets keyed by displayable name."
    )
    default_policy_sets: Set[str] = Field(
        description="PolicySets every new Sandbox is bound to. The app creates, suspends, resumes and deletes "
        "Sandboxes through Kubernetes itself."
    )
    # CLEANUP(added 2026-10-15): Remove after both environments' ConfigMaps omit this key
    #   and have reconciled the omission.
    session_dispatcher_open_sessions: bool = Field(
        default=False, description="Accepted for rollout compatibility; new Session openings always use the Dispatcher."
    )
    egress_admin_url: HttpEndpointUrl = Field(description="The egress proxy's admin port, serving /decisions.")
    action_federation: ActionFederationSettings

    @field_serializer("default_policy_sets")
    def serialize_default_policy_sets(self, value: Set[str]) -> list[str]:
        """Keep the generated ConfigMap's list stable while Settings still parses it as a set."""
        return sorted(value)


class Settings(AppSettingsConfig):
    """The app's complete runtime configuration.

    Each field is a `--flag`, an `AGENTPLANE_*` environment variable, and a key of the YAML file
    `AGENTPLANE_CONFIG_FILE` names, in that order of precedence; the staging Deployment keeps the model
    catalog in that file.
    """

    sandbox_namespace: str = Field(
        description="Namespace whose Sandbox inventory the app observes. Separate from the app's own "
        "so a sandbox shares a namespace with neither the app, its database, nor the rules that govern it."
    )
    runner_gateway_target: str = Field(
        min_length=1, description="The Runner Gateway's gRPC host:port, serving Sessions."
    )
    session_dispatcher_target: str = Field(
        min_length=1, description="Required Session Dispatcher gRPC host:port for Thread commands."
    )
    runner_gateway_control_target: str = Field(
        min_length=1,
        description="Each Sandbox's control Service, a template of `{sandbox}` and `{namespace}` such as "
        "`{sandbox}-control.{namespace}.svc:8080`. Lifecycle calls go there, to the replica holding the "
        "runner connection.",
    )
    history_service_target: str = Field(min_length=1)
    notifications_url: HttpEndpointUrl
    notifications_token_file: Path
    runner_gateway_token_file: Path = Field(
        description="The rotating projected ServiceAccount token for the Runner Gateway audience."
    )
    session_dispatcher_token_file: Path = Field(
        default=Path("/var/run/secrets/agentplane-session-dispatcher/token"),
        description="The rotating projected ServiceAccount token for the Session Dispatcher audience.",
    )
    session_dispatcher_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)
    history_service_token_file: Path = Path("/var/run/secrets/agentplane-history-service/token")
    history_service_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)
    runner_gateway_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)
    runner_gateway_lifecycle_timeout_s: float = Field(default=310, gt=0, allow_inf_nan=False)

    log_levels: LogLevels
    host: str = Field(default="127.0.0.1", description="Bind address.")
    port: int = Field(default=8080, description="Bind port.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    database_url: str = Field(description="SQLAlchemy asyncpg URL of the thread store.")
    electric_url: HttpEndpointUrl = Field(
        description="Cluster-internal root URL of the Electric replicating the History Service fold."
    )
    egress_admin_timeout: float = Field(
        default=5, description="Seconds to wait for the proxy before showing rules only."
    )
    shutdown_timeout: int = Field(
        default=5,
        description="Seconds Uvicorn waits after SIGTERM for open requests and streams before cancelling "
        "them; the rest of the Deployment's grace period is stopping Session openings and closing the database.",
    )
    resync_seconds: int = Field(
        default=300,
        description="Watch lifetime; every kind the live stream pushes is relisted this often, and a "
        "kind that misses several cycles is what the stream reports as stale.",
    )
    token_audience: str = Field(
        default="agentplane",
        description="Audience a Kubernetes token must carry to authenticate here, so none is replayable.",
    )
    token_subjects: frozenset[str] = Field(
        default=frozenset(),
        description="The Kubernetes usernames a token caller may present, as JSON: "
        '["system:serviceaccount:ns:sa"]. A token for any other subject is refused however it was '
        "minted, and the default accepts none at all, leaving an OIDC session the only way in.",
    )
