"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.log_levels import LogLevels
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SANDBOX_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1, description="Audience of the callers' projected tokens.")
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    log_levels: LogLevels
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None
    default_policy_sets: Set[str] = Field(description="PolicySets every new Sandbox is bound to.")
