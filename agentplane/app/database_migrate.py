"""Image-coupled Alembic runner for the integration app's database."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.app.thread.models import Base
from util.db_migrations import MigrationRunner

# `Base` is declared in operator_sessions.py; thread/models.py's own Thread/Event tables and
# operator_sessions.py's BrowserSession all share it, and importing it here (via thread/models.py,
# which already imports operator_sessions.py) registers every table onto one metadata.
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
