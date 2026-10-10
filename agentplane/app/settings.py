"""Agentplane app settings and deployment-authored configuration contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from util.urls import HttpEndpointUrl

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml


# Names the YAML settings file a deployment mounts; not a field, so not a flag.
CONFIG_FILE_ENV = "AGENTPLANE_CONFIG_FILE"


class AppSettingsConfig(BaseSettings):
    """Deployment-authored app settings, before runtime-only inputs arrive.

    The runtime `Settings` model extends this, so cdk8s can construct this typed fragment
    without fabricating the app's namespace, Sandbox Service target, or database URL.
    """

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
    kubernetes_grants: dict[str, KubernetesGrant] = Field(
        default_factory=dict, description="Enabled Kubernetes binding templates; launch requests select names only."
    )
    kubernetes_binding_cleanup_namespaces: list[str] = Field(
        default_factory=list, description="Past namespaced binding scopes retained for cleanup after catalog removal."
    )
    kubernetes_cluster_binding_cleanup: bool = Field(
        default=False, description="Retain cluster binding cleanup after a cluster grant is removed from the catalog."
    )
    default_egress_policies: list[str] = Field(
        default_factory=list,
        description="EgressPolicy names every new sandbox is granted before the caller's own picks: "
        "what no sandbox works without, the model endpoint above all.",
    )
    sandbox_service_grpc_channel_options: dict[str, int | str] = Field(
        default_factory=dict, description="gRPC channel options for the App's connection to Sandbox Service."
    )
    egress_admin_url: HttpEndpointUrl = Field(description="The egress proxy's admin port, serving /decisions.")
    action_federation: ActionFederationSettings | None = None


class Settings(AppSettingsConfig):
    """The app's complete runtime configuration.

    Each field is a `--flag`, an `AGENTPLANE_*` environment variable, and a key of the YAML file
    `AGENTPLANE_CONFIG_FILE` names, in that order of precedence; the staging Deployment keeps the model
    catalog in that file.
    """

    namespace: str = Field(description="The app's own namespace, holding the egress policies and bindings.")
    sandbox_namespace: str = Field(
        description="Namespace whose Sandbox inventory the app observes. Separate from the app's own "
        "so a sandbox shares a namespace with neither the app, its database, nor the rules that govern it."
    )
    sandbox_service_target: str = Field(min_length=1)
    notifications_url: HttpEndpointUrl | None = None
    notifications_token_file: Path | None = None
    sandbox_service_token_file: Path = Path("/var/run/secrets/agentplane-sandbox-service/token")
    sandbox_service_request_timeout_s: float = Field(default=20, gt=0, allow_inf_nan=False)
    history_reads_enabled: bool = False
    # Projects fenced Threads and creates new service-backed Threads fenced at zero.
    # Existing Threads require explicit handoff; never bulk-fence on startup.
    history_projection_enabled: bool = False
    sandbox_service_lifecycle_timeout_s: float = Field(default=310, gt=0, allow_inf_nan=False)
    sandbox_service_follow_timeout_s: float = Field(default=960, gt=0, allow_inf_nan=False)
    sandbox_service_command_admission_timeout_s: float = Field(
        default=20,
        gt=0,
        allow_inf_nan=False,
        description="Seconds the app waits for the Sandbox Service to return the runner's durable "
        "command admission receipt. Keep above its runner_admission_ack_timeout_s setting.",
    )
    host: str = Field(default="127.0.0.1", description="Bind address.")
    port: int = Field(default=8080, description="Bind port.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    database_url: str = Field(description="SQLAlchemy asyncpg URL of the thread store.")
    electric_url: HttpEndpointUrl | None = Field(
        default=None, description="Cluster-internal Electric root URL; omitted leaves thread sync routes disabled."
    )
    egress_admin_timeout: float = Field(
        default=5, description="Seconds to wait for the proxy before showing rules only."
    )
    shutdown_timeout: int = Field(
        default=5,
        description="Seconds Uvicorn waits after SIGTERM for open requests and streams before cancelling "
        "them; the rest of the Deployment's grace period is the ingester's lease release and closing the database.",
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

    def __init__(self, **values: Any) -> None:
        # BaseSettings fills required fields from its sources; spell that out because the mypy plugin
        # derives a required-argument signature from the fields.
        super().__init__(**values)

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
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
