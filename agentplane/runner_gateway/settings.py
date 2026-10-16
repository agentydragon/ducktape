"""Runner Gateway deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.log_levels import LogLevels
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings

RUNNER_CONTROL_AUDIENCE = "agentplane-runner-control"


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_RUNNER_GATEWAY_", cli_parse_args=True, cli_kebab_case=True)

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1, description="Audience of the callers' projected tokens.")
    sandbox_controller_account: ServiceAccountRef | None = Field(
        default=None,
        description="The Sandbox controller's account, the one caller of the proxied `Runner.Quiesce`; "
        "its tokens carry `token_audience`. Without it nobody may seal or quiesce through the proxy.",
    )
    log_levels: LogLevels
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None
    # Old Pod configs may still supply this while the migration init container uses the same config.
    # The serving process no longer opens the database; physical retirement is a later rollout gate.
    commands_database_url: str | None = Field(
        default=None, description="Legacy database URL accepted during rollout; not used by the serving process."
    )
    runner_control_audience: str = Field(
        default=RUNNER_CONTROL_AUDIENCE,
        description="Audience of the Pod tokens the egress proxy substitutes on runner control stream connections.",
    )
    runner_control_port: int = Field(default=8082, ge=1, le=65535)
    pod_ip: str = Field(
        min_length=1,
        description="This replica's Pod IP, where a Sandbox's control Service points while it holds the runner connection.",
    )
