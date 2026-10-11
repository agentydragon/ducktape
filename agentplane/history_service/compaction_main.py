"""One-off Job entry point: report, or with `--apply` perform, settlement of stored history."""

import asyncio
import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.compaction import compact

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


class CompactionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_HISTORY_COMPACTION_", cli_parse_args=True, cli_kebab_case=True
    )

    database_url: str = Field(min_length=1)
    apply: bool = Field(default=False, description="Write settlements; without it the run only reports them.")
    batch_size: int = Field(
        default=1000, ge=1, le=10_000, description="Entries read per transaction, each under the Session's lock."
    )


async def run(settings: CompactionSettings) -> None:
    engine = create_async_engine(make_url(settings.database_url).set(drivername="postgresql+asyncpg"))
    try:
        await compact(engine, apply=settings.apply, batch_size=settings.batch_size)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(CompactionSettings()))


if __name__ == "__main__":
    main()
