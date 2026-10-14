"""Image-owned, advisory-locked Alembic migrations for the Session Dispatcher database."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.session_dispatcher.db import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4453_5043,  # "DSPC"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_SESSION_DISPATCHER_")
    database_url: str


def main() -> None:
    RUNNER.apply(Settings().database_url)


if __name__ == "__main__":
    main()
