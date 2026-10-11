"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml
# Aliased: this module defines its own `Settings`.
from agentplane.runner_gateway.settings import RUNNER_CHANNEL_AUDIENCE, Settings as GatewaySettings
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef

CONFIG_FILE_ENV = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"
_ENV_PREFIX = "AGENTPLANE_SANDBOX_SERVICE_"


class _FromConfigFile(BaseSettings):
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


class Launch(_FromConfigFile):
    """Whether this process seals through a Runner Gateway, or runs the gateway itself."""

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, extra="ignore")

    # CLEANUP(added 2026-10-11): Delete with `CombinedSettings` and `main.serve_combined` once the
    # manifests run the Runner Gateway Deployment (SANDBOX_SERVICE_DEPLOYMENT_SPLIT's manifest PR).
    gateway_target: str | None = None


class _Provisioning(_FromConfigFile):
    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, cli_parse_args=True, cli_kebab_case=True)

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = "agentplane-sandbox-service"
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    health_port: int = Field(default=8081, ge=1, le=65535)
    kubeconfig: Path | None = None

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    default_egress_policies: list[str] = Field(default_factory=list)
    kubernetes_grants: dict[str, KubernetesGrant] = Field(default_factory=dict)
    kubernetes_binding_cleanup_namespaces: set[str] = Field(default_factory=set)
    kubernetes_cluster_binding_cleanup: bool = False
    # CLEANUP(added 2026-10-11): require `runner_control` and `runner_ingress` once both environments'
    #   Sandbox Service config sets them and no SandboxTemplate passes --channel-target.
    runner_control: RunnerEndpoint | None = Field(
        default=None,
        description="The Runner Gateway endpoint every new Sandbox's runner dials for its control stream. "
        "Unset, runners dial whatever their template names.",
    )
    runner_ingress: RunnerEndpoint | None = Field(
        default=None,
        description="The ingress every new Sandbox's runner pushes its Events to. Unset, runners start without one.",
    )


class Settings(_Provisioning):
    gateway_target: str = Field(min_length=1, description="The Runner Gateway's Service, as `host:port`.")
    control_target: str = Field(
        min_length=1,
        description="A Sandbox's control Service, which reaches the gateway replica holding its runner connection, "
        "as a template of `{sandbox}` and `{namespace}`.",
    )
    gateway_token_file: Path = Field(
        description="This Pod's projected token for the gateway, of audience `token_audience`; the gateway accepts "
        "it from the Sandbox Service alone for `SealSandbox`."
    )


class CombinedSettings(_Provisioning):
    """The one Deployment that also runs the Runner Gateway (`Launch.gateway_target` unset). It reads
    the gateway's keys under the Sandbox Service's own prefix and config file."""

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, cli_parse_args=True, cli_kebab_case=True)

    commands_database_url: str = Field(min_length=1)
    platform_instructions: str = Field(min_length=1)
    runner_channel_audience: str = RUNNER_CHANNEL_AUDIENCE
    runner_channel_port: int = Field(default=8082, ge=1, le=65535)
    runner_port: int = Field(default=7000, ge=1, le=65535)
    runner_admission_ack_timeout_s: float = Field(default=15, gt=0)
    follow_lease_s: float = Field(default=900, gt=0, le=900)
    runner_grpc_channel_options: dict[str, int | str] = Field(default_factory=dict)
    pod_ip: str = Field(min_length=1)
    # CLEANUP(added 2026-10-10): Nothing reads `database_url` or `ingest_history`; the History Service
    #   writes Session history. Delete both with the manifest keys that set them once the release that
    #   stopped reading them serves on staging and testing, so no older Pod is left requiring them.
    database_url: str | None = None
    ingest_history: Literal[False] = False

    def gateway_settings(self) -> GatewaySettings:
        """The in-process gateway's settings. It seals in-process, so no account may seal over the API."""
        return GatewaySettings(
            _cli_parse_args=False,
            sealer_accounts=frozenset(),
            sandbox_namespace=self.sandbox_namespace,
            caller_accounts=self.caller_accounts,
            token_audience=self.token_audience,
            admission_timeout_s=self.admission_timeout_s,
            lifecycle_timeout_s=self.lifecycle_timeout_s,
            host=self.host,
            port=self.port,
            health_port=self.health_port,
            kubeconfig=self.kubeconfig,
            commands_database_url=self.commands_database_url,
            platform_instructions=self.platform_instructions,
            runner_channel_audience=self.runner_channel_audience,
            runner_channel_port=self.runner_channel_port,
            runner_port=self.runner_port,
            runner_admission_ack_timeout_s=self.runner_admission_ack_timeout_s,
            follow_lease_s=self.follow_lease_s,
            runner_grpc_channel_options=self.runner_grpc_channel_options,
            pod_ip=self.pod_ip,
        )
