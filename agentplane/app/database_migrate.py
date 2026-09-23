"""Image-coupled Alembic runner for the integration app's database."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every module declaring tables on `Base`, imported only to register them on its metadata.
from agentplane.app import operator_sessions  # noqa: F401
from agentplane.app.agent_runtime import models  # noqa: F401
from agentplane.app.database import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x5452_414A,  # "TRAJ"
    version_table="alembic_version_app",
)


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_")

    database_url: str


def main() -> None:
    RUNNER.apply(MigrationSettings().database_url)


if __name__ == "__main__":
    main()
