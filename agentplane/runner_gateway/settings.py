"""Runner Gateway deployment settings and configuration file name."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agentplane.subjects import ServiceAccountRef

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

CONFIG_FILE_ENV = "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
RUNNER_CONTROL_AUDIENCE = "agentplane-runner-control"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_RUNNER_GATEWAY_", cli_parse_args=True, cli_kebab_case=True)

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = "agentplane-sandbox-service"
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None
    commands_database_url: str = Field(
        min_length=1, description="The `sandbox_commands` database holding command admission."
    )
    platform_instructions: str = Field(min_length=1)
    runner_control_audience: str = Field(
        default=RUNNER_CONTROL_AUDIENCE,
        description="Audience of the Pod tokens the egress proxy substitutes on runner control stream connections.",
    )
    runner_control_port: int = Field(default=8082, ge=1, le=65535)
    runner_port: int = Field(default=7000, ge=1, le=65535)
    runner_admission_ack_timeout_s: float = Field(
        default=15,
        gt=0,
        description="Seconds SubmitCommand waits for the runner journal to durably record the exact "
        "command; expiry leaves admission uncertain.",
    )
    follow_lease_s: float = Field(default=900, gt=0, le=900)
    runner_grpc_channel_options: dict[str, int | str] = Field(
        default_factory=dict,
        description="gRPC options for gateway-to-runner control streams; receives retained journal events up to the configured limit.",
    )
    pod_ip: str = Field(
        min_length=1,
        description="This replica's Pod IP, where a Sandbox's control Service points while it holds the runner connection.",
    )
    sealer_accounts: frozenset[ServiceAccountRef] = Field(
        description="The Sandbox Service's account, which alone may seal a Sandbox for teardown."
    )

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
