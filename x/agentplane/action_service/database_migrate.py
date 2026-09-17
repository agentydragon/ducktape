"""Image-coupled Alembic runner for the Action Service-owned database."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from util.db_migrations import MigrationRunner
from x.agentplane.action_service.db import Base

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4143_544E,  # "ACTN"
)


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_ACTIONS_")

    database_url: str


def main() -> None:
    RUNNER.apply(MigrationSettings().database_url)


if __name__ == "__main__":
    main()
