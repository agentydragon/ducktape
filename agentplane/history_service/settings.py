"""History Service deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import SettingsConfigDict

from agentplane.log_levels import LogLevels
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings

RUNNER_INGRESS_AUDIENCE = "agentplane-runner-ingress"


class IngressSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_audience: str = Field(
        min_length=1, description="The audience runners' projected tokens carry for the ingress."
    )
    writer_namespaces: frozenset[str] = Field(
        min_length=1, description="Namespaces whose ServiceAccounts may push: the ones Sandboxes run in."
    )
    listen_address: str = Field(
        default="0.0.0.0:8081",
        min_length=1,
        description="The ingress listener, so the network fence admits the egress proxy to it and nothing else.",
    )
    settle_deltas: bool = Field(
        default=True,
        description="Remove an item's streamed delta entries from stored history once its retained "
        "completion provably holds their concatenation. Turn off to keep every entry for debugging.",
    )


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_HISTORY_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    database_url: str = Field(min_length=1)
    reader_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1)
    request_timeout_s: float = Field(default=15, gt=0, le=60)
    log_levels: LogLevels
    listen_address: str = Field(default="0.0.0.0:8080", min_length=1)
    kubeconfig: Path | None = None
    ingress: IngressSettings
    fold_concurrency: int = Field(default=4, ge=1, le=64)
    # A backfill advances each Session one page per cycle; the history store serves at most 1000.
    fold_page_size: int = Field(default=1000, ge=1, le=1000)
    fold_interval_s: float = Field(default=2, gt=0)
    native_frame_read_batch_size: int = Field(
        default=200, ge=1, description="Maximum linked sequences per raw-history query, independent of API pagination."
    )
    watch_lease_s: float = Field(
        default=300, gt=0, description="Seconds a WatchSessionSummaries stream lasts before planned renewal."
    )
