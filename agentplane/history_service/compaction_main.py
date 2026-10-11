"""One-off Job entry point: report, or with `--apply` perform, settlement of stored history."""

import asyncio
import logging

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.compaction import compact
from agentplane.history_service.compaction_settings import CompactionSettings

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


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
