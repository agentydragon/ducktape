"""Session Dispatcher deployment settings and configuration file name."""

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.log_levels import LogLevels
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_SESSION_DISPATCHER_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SESSION_DISPATCHER_", cli_parse_args=True, cli_kebab_case=True
    )

    database_url: str = Field(min_length=1)
    namespace: str = Field(min_length=1, description="The namespace Sandboxes live in, which the Dispatcher watches.")
    caller_accounts: frozenset[ServiceAccountRef] = Field(
        min_length=1, description="ServiceAccounts that may send commands."
    )
    token_audience: str = Field(min_length=1, description="The audience callers' bearers carry for the Dispatcher.")
    runner_control_target: str = Field(
        min_length=1, description="Where a Sandbox's runner is reached, as a template of `{sandbox}` and `{namespace}`."
    )
    runner_token_file: Path = Field(
        description="The projected ServiceAccount token the Runner Gateway admits; reread for each call."
    )
    runner_request_timeout_s: float = Field(default=10, gt=0, le=60)
    request_timeout_s: float = Field(default=15, gt=0, le=60)
    log_levels: LogLevels
    max_queued_per_session: int = Field(default=1000, ge=1)
    sandbox_resync_seconds: int = Field(default=300, ge=1)
    watch_lease_s: float = Field(default=300, gt=0, description="Seconds a WatchDispatches stream lasts.")
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None
