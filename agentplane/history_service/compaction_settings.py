"""Settings of the one-off Job that settles stored history."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CompactionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_HISTORY_COMPACTION_", cli_parse_args=True, cli_kebab_case=True
    )

    database_url: str = Field(min_length=1)
    apply: bool = Field(default=False, description="Write settlements; without it the run only reports them.")
    batch_size: int = Field(
        default=1000, ge=1, le=10_000, description="Entries read per transaction, each under the Session's lock."
    )
