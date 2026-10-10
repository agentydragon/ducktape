"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef

CONFIG_FILE_ENV = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"
RUNNER_CHANNEL_AUDIENCE = "agentplane-runner-channel"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SANDBOX_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    sandbox_namespace: str = Field(min_length=1)
    # CLEANUP(added 2026-10-10): Nothing reads `database_url` or `ingest_history`; the History Service
    #   writes Session history. Delete both with the manifest keys that set them once the release that
    #   stopped reading them serves on staging and testing, so no older Pod is left requiring them.
    database_url: str | None = None
    commands_database_url: str = Field(
        min_length=1, description="The `sandbox_commands` database holding command admission."
    )
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    platform_instructions: str = Field(min_length=1)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    ingest_history: Literal[False] = False
    default_egress_policies: list[str] = Field(default_factory=list)
    kubernetes_grants: dict[str, KubernetesGrant] = Field(default_factory=dict)
    kubernetes_binding_cleanup_namespaces: set[str] = Field(default_factory=set)
    kubernetes_cluster_binding_cleanup: bool = False
    token_audience: str = "agentplane-sandbox-service"
    runner_channel_audience: str = Field(
        default=RUNNER_CHANNEL_AUDIENCE,
        description="Audience of the Pod tokens the egress proxy substitutes on runner channel connections.",
    )
    runner_channel_port: int = Field(default=8082, ge=1, le=65535)
    # CLEANUP(added 2026-10-11): require `runner_control` and `runner_ingress` once both environments'
    #   Sandbox Service config sets them and no SandboxTemplate passes --channel-target.
    runner_control: RunnerEndpoint | None = Field(
        default=None,
        description="The hub endpoint every new Sandbox's runner dials for its control stream. "
        "Unset, runners dial whatever their template names.",
    )
    runner_ingress: RunnerEndpoint | None = Field(
        default=None,
        description="The ingress every new Sandbox's runner pushes its Events to. Unset, runners start without one.",
    )
    runner_port: int = Field(default=7000, ge=1, le=65535)
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    runner_admission_ack_timeout_s: float = Field(
        default=15,
        gt=0,
        description="Seconds SubmitCommand waits for the runner journal to durably record the exact "
        "command; expiry leaves admission uncertain.",
    )
    follow_lease_s: float = Field(default=900, gt=0, le=900)
    runner_grpc_channel_options: dict[str, int | str] = Field(
        default_factory=dict,
        description="gRPC options for Sandbox Service-to-runner channels; receives retained journal events up to the configured limit.",
    )
    pod_ip: str = Field(
        min_length=1,
        description="This replica's Pod IP, where a Sandbox's control Service points while it holds the runner connection.",
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    health_port: int = Field(default=8081, ge=1, le=65535)
    kubeconfig: Path | None = None

    def __init__(self, **values: Any) -> None:
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
        sources = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
