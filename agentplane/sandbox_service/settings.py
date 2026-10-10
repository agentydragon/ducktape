"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from agentplane.subjects import ServiceAccountRef

CONFIG_FILE_ENV = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SANDBOX_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    sandbox_namespace: str = Field(min_length=1)
    database_url: str | None = Field(default=None, min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    history_reader_accounts: frozenset[ServiceAccountRef] = frozenset()
    platform_instructions: str = Field(min_length=1)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    ingest_history: bool = Field(
        description="Copy runner journals into the history tables. Off once the History Service ingester writes them."
    )
    default_egress_policies: list[str] = Field(default_factory=list)
    kubernetes_grants: dict[str, KubernetesGrant] = Field(default_factory=dict)
    kubernetes_binding_cleanup_namespaces: set[str] = Field(default_factory=set)
    kubernetes_cluster_binding_cleanup: bool = False
    token_audience: str = "agentplane-sandbox-service"
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
