"""Runner Gateway deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings

RUNNER_CONTROL_AUDIENCE = "agentplane-runner-control"


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_RUNNER_GATEWAY_", cli_parse_args=True, cli_kebab_case=True)

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1, description="Audience of the callers' projected tokens.")
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
